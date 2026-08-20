# -*- coding: utf-8 -*-
"""后台轮询器：自动发现并激活云端已批准的新模型版本。

轮询 ``/cloud/model-update/pending-distribution`` 获取两类待办：
- ``pending_pulls``：已批准、待分发的新版本 → 调用 ``pull_and_activate``
  下载、校验并原子激活，随后回传分发结果；
- ``pending_rollbacks``：云端已请求回滚的版本 → 调用 ``rollback_version``
  把本地激活指针切回基线。

任一步失败都保留旧版本不变（``pull_and_activate`` 原子性保证），并回传
失败结果供云端标记分发失败。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from edge_model.model_pull import ModelPullError, pull_and_activate, rollback_version


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
        self._processed: set[str] = set()
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
            processed = data.get("processed_update_ids")
            if isinstance(processed, list):
                self._processed = {str(item) for item in processed}
        except (OSError, ValueError, json.JSONDecodeError):
            self._processed = set()

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {"processed_update_ids": sorted(self._processed)},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
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
                "processed_update_ids": sorted(self._processed),
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
                self._last_error_code = None
                self._last_round_duration_ms = duration_ms
                self._last_summary = summary
            return True
        finally:
            self._round_lock.release()

    def poll_once(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "pending_pulls": 0,
            "activated": 0,
            "pull_failed": 0,
            "pending_rollbacks": 0,
            "rolled_back": 0,
            "rollback_skipped": 0,
            "rollback_failed": 0,
        }
        try:
            pending = self._fetch_pending()
        except Exception as error:
            summary["error"] = "PENDING_QUERY_FAILED: %r" % (error,)
            return summary
        for item in pending.get("pending_pulls", []):
            summary["pending_pulls"] += 1
            update_id = item.get("update_id")
            if not isinstance(update_id, str) or update_id in self._processed:
                continue
            try:
                pull_and_activate(
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
            except (ModelPullError, KeyError, ValueError) as error:
                summary["pull_failed"] += 1
                self.logger.warning("模型 %s 拉取激活失败: %s", update_id, error)
                self._report_distribution(
                    update_id, "failed", message=str(error)
                )
                continue
            try:
                self._report_distribution(update_id, "succeeded")
            except Exception as error:
                self.logger.warning("模型 %s 分发结果回传失败: %s", update_id, error)
            self._mark_processed(update_id)
            summary["activated"] += 1
        for item in pending.get("pending_rollbacks", []):
            summary["pending_rollbacks"] += 1
            update_id = item.get("update_id")
            if not isinstance(update_id, str) or update_id in self._processed:
                continue
            try:
                rollback_version(
                    model_type=item.get("model_type", "distilled_h5"),
                    target_version=item["rollback_target_version"],
                    model_root=self.model_root,
                )
            except ModelPullError as error:
                if "ROLLBACK_VERSION_NOT_INSTALLED" in str(error):
                    summary["rollback_skipped"] += 1
                else:
                    summary["rollback_failed"] += 1
                    self.logger.warning("模型 %s 回滚失败: %s", update_id, error)
                continue
            self._mark_processed(update_id)
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

    def _mark_processed(self, update_id: str) -> None:
        with self._lock:
            self._processed.add(update_id)
        self._save_state()

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = self.monotonic()
            self.run_once()
            elapsed = max(self.monotonic() - started, 0.0)
            self._stop.wait(max(self.poll_interval_seconds - elapsed, 0.0))
