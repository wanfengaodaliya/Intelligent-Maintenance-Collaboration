# -*- coding: utf-8 -*-
"""后台轮询器：自动发现并激活云端已批准的新模型版本。

轮询 ``/cloud/model-update/pending-distribution`` 获取两类待办：
- ``pending_pulls``：下载并校验候选，再由本地 H5 运行时执行探针和原子切换；
- ``pending_rollbacks``：由同一运行时切换入口回到已安装版本。

只有运行 handle 与持久化 active 指针都确认目标版本后才回传成功。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from edge_model.local_h5_client import H5ActivationError
from edge_model.model_pull import ModelPullError, pull_candidate


def _default_http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
        return response.read()


def _default_http_post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


class ModelUpdatePoller:
    """周期轮询云端模型更新并推进本地激活/回滚，单轮异常只影响当前一轮。"""

    def __init__(
        self,
        *,
        cloud_base_url: str,
        edge_node_id: str,
        model_root: Path | str,
        model_runtime: Any,
        poll_interval_seconds: float = 30.0,
        state_path: Path | str | None = None,
        http_get: Callable[[str], bytes] | None = None,
        http_post: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.cloud_base_url = cloud_base_url.rstrip("/")
        self.edge_node_id = edge_node_id
        self.model_root = Path(model_root)
        self.model_runtime = model_runtime
        self.poll_interval_seconds = poll_interval_seconds
        self.state_path = Path(state_path) if state_path else self.model_root / ".model_update_state.json"
        self.http_get = http_get or _default_http_get
        self.http_post = http_post or _default_http_post
        self.clock_ns = clock_ns
        self.monotonic = monotonic
        self.logger = logger or logging.getLogger(__name__)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._round_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._activated: set[str] = set()
        self._rolled_back: set[str] = set()
        self._pending_report: set[str] = set()
        self._last_success_at_ns: int | None = None
        self._last_error_code: str | None = None
        self._last_round_duration_ms = 0.0
        self._last_summary: dict[str, Any] | None = None
        self._rounds_total = 0
        self._errors_total = 0
        self._load_state()

    def _load_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            self._activated = set()
            self._rolled_back = set()
            self._pending_report = set()
            return
        activated = data.get("activated_update_ids")
        if isinstance(activated, list):
            self._activated = {str(item) for item in activated}
        else:
            # 旧格式：processed_update_ids 视为已激活（仍允许后续回滚）。
            legacy = data.get("processed_update_ids")
            self._activated = (
                {str(item) for item in legacy} if isinstance(legacy, list) else set()
            )
        rolled_back = data.get("rolled_back_update_ids")
        self._rolled_back = (
            {str(item) for item in rolled_back}
            if isinstance(rolled_back, list)
            else set()
        )
        pending_report = data.get("pending_report_update_ids")
        self._pending_report = (
            {str(item) for item in pending_report}
            if isinstance(pending_report, list)
            else set()
        )

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            payload = json.dumps(
                {
                    "activated_update_ids": sorted(self._activated),
                    "rolled_back_update_ids": sorted(self._rolled_back),
                    "pending_report_update_ids": sorted(self._pending_report),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        except OSError:
            self.logger.warning("模型更新状态持久化失败: %s", self.state_path)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="edge-model-update-poller", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout_seconds: float | None = None) -> None:
        with self._lock:
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None:
            thread.join(timeout=timeout_seconds or self.poll_interval_seconds + 2.0)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def health(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "model_update_poller_running": running,
                "last_success_at_ns": self._last_success_at_ns,
                "last_error_code": self._last_error_code,
                "last_round_duration_ms": self._last_round_duration_ms,
                "last_summary": dict(self._last_summary) if self._last_summary else None,
                "rounds_total": self._rounds_total,
                "errors_total": self._errors_total,
                "processed_update_ids": sorted(self._activated | self._rolled_back),
                "pending_report_update_ids": sorted(self._pending_report),
            }

    def run_once(self) -> bool:
        """同步执行一轮轮询；与后台轮次互斥，返回是否实际执行。"""
        if not self._round_lock.acquire(blocking=False):
            return False
        try:
            started = self.monotonic()
            try:
                summary = self.poll_once()
            except Exception as error:
                duration_ms = max((self.monotonic() - started) * 1000.0, 0.0)
                with self._lock:
                    self._errors_total += 1
                    self._last_error_code = getattr(error, "code", type(error).__name__)
                    self._last_round_duration_ms = duration_ms
                self.logger.warning("模型更新轮询失败: %s", error)
                return True
            duration_ms = max((self.monotonic() - started) * 1000.0, 0.0)
            with self._lock:
                self._rounds_total += 1
                self._last_success_at_ns = self.clock_ns()
                self._last_error_code = summary.get("last_error_code")
                self._last_round_duration_ms = duration_ms
                self._last_summary = summary
            return True
        finally:
            self._round_lock.release()

    def poll_once(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "pending_pulls": 0,
            "activated": 0,
            "reported": 0,
            "pull_failed": 0,
            "pending_rollbacks": 0,
            "rolled_back": 0,
            "rollback_skipped": 0,
            "rollback_failed": 0,
            "rollback_ack_failed": 0,
        }
        try:
            pending = self._fetch_pending()
        except Exception as error:
            summary["error"] = "PENDING_QUERY_FAILED: %r" % (error,)
            summary["last_error_code"] = "PENDING_QUERY_FAILED"
            return summary
        for item in pending.get("pending_pulls", []):
            summary["pending_pulls"] += 1
            update_id = item.get("update_id")
            if not isinstance(update_id, str):
                continue
            if update_id in self._activated and update_id not in self._pending_report:
                continue
            if update_id not in self._activated:
                try:
                    pull_candidate(
                        update_id=update_id,
                        download_url=(
                            f"{self.cloud_base_url}/cloud/model-update/{update_id}/file"
                        ),
                        target_version=item["candidate_version"],
                        model_type=item.get("model_type", "distilled_h5"),
                        model_root=self.model_root,
                        expected_sha256=item.get("artifact_sha256"),
                        http_get=self.http_get,
                    )
                    activation = self.model_runtime.activate_version(
                        item["candidate_version"]
                    )
                    if (
                        activation.get("runtime_version") != item["candidate_version"]
                        or activation.get("active_pointer_version")
                        != item["candidate_version"]
                    ):
                        raise H5ActivationError("MODEL_ACTIVATION_CONFIRMATION_FAILED")
                except (ModelPullError, H5ActivationError, KeyError, ValueError) as error:
                    summary["pull_failed"] += 1
                    summary["last_error_code"] = _stable_error_code(error)
                    self.logger.warning("模型 %s 拉取激活失败: %s", update_id, error)
                    self._report_distribution(
                        update_id, "failed", message=str(error)
                    )
                    continue
                self._activated.add(update_id)
                self._pending_report.add(update_id)
                self._save_state()
                summary["activated"] += 1
            try:
                self._report_distribution(update_id, "succeeded")
                self._pending_report.discard(update_id)
                summary["reported"] += 1
            except Exception as error:
                self._pending_report.add(update_id)
                summary["last_error_code"] = "DISTRIBUTION_REPORT_FAILED"
                self.logger.warning("模型 %s 分发结果回传失败: %s", update_id, error)
            self._save_state()
        for item in pending.get("pending_rollbacks", []):
            summary["pending_rollbacks"] += 1
            update_id = item.get("update_id")
            if not isinstance(update_id, str) or update_id in self._rolled_back:
                continue
            try:
                target_version = item["rollback_target_version"]
                activation = self.model_runtime.activate_version(target_version)
                if (
                    activation.get("runtime_version") != target_version
                    or activation.get("active_pointer_version") != target_version
                ):
                    raise H5ActivationError("MODEL_ROLLBACK_CONFIRMATION_FAILED")
            except (H5ActivationError, KeyError, ValueError) as error:
                summary["last_error_code"] = _stable_error_code(error)
                if "MODEL_MANIFEST_DIR_MISSING" in str(error):
                    summary["rollback_skipped"] += 1
                else:
                    summary["rollback_failed"] += 1
                    self.logger.warning("模型 %s 回滚失败: %s", update_id, error)
                continue
            try:
                self._report_rollback(
                    update_id,
                    rollback_target_version=item["rollback_target_version"],
                )
            except Exception as error:
                summary["rollback_ack_failed"] += 1
                summary["last_error_code"] = "ROLLBACK_ACK_FAILED"
                self.logger.warning("模型 %s 回滚确认回传失败: %s", update_id, error)
                continue
            self._rolled_back.add(update_id)
            self._pending_report.discard(update_id)
            self._save_state()
            summary["rolled_back"] += 1
        return summary

    def _fetch_pending(self) -> dict[str, Any]:
        url = (
            f"{self.cloud_base_url}/cloud/model-update/pending-distribution"
            f"?edge_node_id={self.edge_node_id}"
        )
        payload = self.http_get(url)
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("PENDING_DISTRIBUTION_RESPONSE_INVALID")
        return data

    def _report_distribution(
        self, update_id: str, status: str, *, message: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"status": status}
        if message:
            body["message"] = message
        url = (
            f"{self.cloud_base_url}/cloud/model-update/{update_id}/distribution-result"
        )
        return self.http_post(url, body)

    def _report_rollback(
        self, update_id: str, *, rollback_target_version: str
    ) -> dict[str, Any]:
        url = f"{self.cloud_base_url}/cloud/model-update/{update_id}/rollback-result"
        return self.http_post(
            url,
            {
                "status": "succeeded",
                "edge_node_id": self.edge_node_id,
                "rollback_target_version": rollback_target_version,
            },
        )

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = self.monotonic()
            self.run_once()
            elapsed = max(self.monotonic() - started, 0.0)
            self._stop.wait(max(self.poll_interval_seconds - elapsed, 0.0))


def _stable_error_code(error: Exception) -> str:
    text = str(error).strip()
    if text:
        return text.split(":", 1)[0].split("=", 1)[0].strip()
    return type(error).__name__
