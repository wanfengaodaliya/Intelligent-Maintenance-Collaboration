"""Core Toxiproxy plugin lifecycle and per-link apply orchestration."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterable
from contextlib import contextmanager
import logging
from threading import Condition, Lock, RLock
import time
from typing import Iterator

from controller.config_loader import ResolvedLinkConfig
from domain.exceptions import (
    ProxyConflictError,
    ProxyNotFoundError,
    ToxiproxyUnavailableError,
)
from domain.models import ApplyResult, NetworkParameters
from plugins.base import BasePlugin, PluginContext

from .client import ToxiproxyClient
from .state_applier import StateApplier


class ToxiproxyPlugin(BasePlugin):
    name = "toxiproxy"
    dependencies = ("logger",)
    required = True

    def __init__(
        self,
        client: ToxiproxyClient,
        *,
        applier: StateApplier | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now_ns: Callable[[], int] = time.time_ns,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._applier = applier or StateApplier(client)
        self._monotonic = monotonic
        self._now_ns = now_ns
        self._logger = logger or logging.getLogger(__name__)
        self._context: PluginContext | None = None
        self._links: dict[str, ResolvedLinkConfig] = {}
        self._link_locks: dict[str, Lock] = {}
        self._managed_link_ids: list[str] = []
        self._pending_initial_apply_ids: set[str] = set()
        self._started = False
        self._stopping = False
        self._stopped = False
        self._lifecycle_lock = RLock()
        self._active_condition = Condition(self._lifecycle_lock)
        self._active_applies = 0
        self._executor: ThreadPoolExecutor | None = None

    def initialize(self, context: PluginContext) -> None:
        if self._context is not None:
            raise RuntimeError("Toxiproxy plugin is already initialized")
        links = {link.link_id: link for link in context.config.links}
        runtime_ids = {link.link_id for link in context.runtime_store.list_links()}
        if set(links) != runtime_ids:
            raise ValueError("configured links and runtime links do not match")

        self._context = context
        self._links = links
        self._link_locks = {link_id: Lock() for link_id in links}
        self._wait_until_healthy(context.config.toxiproxy.startup_wait_seconds)
        for link in links.values():
            self._client.ensure_proxy(link.proxy_name, link.listen, link.upstream)
            self._managed_link_ids.append(link.link_id)

        timestamp_ns = self._now_ns()
        for link_id in self._managed_link_ids:
            try:
                result = self._apply_and_store(
                    link_id,
                    generation=0,
                    timestamp_ns=timestamp_ns,
                )
            except Exception as exc:
                error = (
                    f"{type(exc).__name__}: initial apply orchestration failed"
                )
                try:
                    context.runtime_store.update_apply_result(
                        link_id,
                        success=False,
                        applied_parameters=None,
                        timestamp_ns=timestamp_ns,
                        generation=0,
                        error=error,
                    )
                except Exception as store_exc:
                    self._logger.error(
                        "failed to record initial Toxiproxy apply failure",
                        extra={
                            "link_id": link_id,
                            "error_type": type(store_exc).__name__,
                        },
                    )
                result = ApplyResult(
                    link_id=link_id,
                    success=False,
                    applied_parameters=None,
                    timestamp_ns=timestamp_ns,
                    error=error,
                )
            if not result.success:
                self._pending_initial_apply_ids.add(link_id)
        self._stopped = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._context is None:
                raise RuntimeError("Toxiproxy plugin must be initialized before start")
            if self._started:
                raise RuntimeError("Toxiproxy plugin is already started")
            if self._stopping:
                raise RuntimeError("Toxiproxy plugin is stopping")
            if self._stopped:
                raise RuntimeError("stopped Toxiproxy plugin cannot be restarted")
            assert self._context is not None
            self._executor = ThreadPoolExecutor(
                max_workers=self._context.config.controller.max_parallel_updates
            )
            self._started = True
            self._stopped = False

    def apply(
        self,
        link_id: str,
        *,
        generation: int,
        timestamp_ns: int,
    ) -> ApplyResult:
        with self._active_apply():
            return self._apply_and_store(link_id, generation, timestamp_ns)

    def apply_all(
        self,
        *,
        generation: int,
        timestamp_ns: int,
        link_ids: Iterable[str] | None = None,
    ) -> tuple[ApplyResult, ...]:
        with self._lifecycle_lock:
            if not self._started or self._stopping or self._context is None:
                raise RuntimeError("Toxiproxy plugin must be started before apply")
            executor = self._executor
            if executor is None:
                raise RuntimeError("Toxiproxy apply executor is unavailable")
        selected = tuple(self._links if link_ids is None else link_ids)
        unknown = set(selected) - set(self._links)
        if unknown:
            raise KeyError(f"unknown link_ids: {sorted(unknown)}")
        if not selected:
            return ()

        def apply_one(link_id: str) -> ApplyResult:
            try:
                return self.apply(
                    link_id, generation=generation, timestamp_ns=timestamp_ns
                )
            except Exception as exc:
                try:
                    assert self._context is not None
                    self._context.runtime_store.update_apply_result(
                        link_id,
                        success=False,
                        applied_parameters=None,
                        timestamp_ns=timestamp_ns,
                        generation=generation,
                        error=f"{type(exc).__name__}: apply orchestration failed",
                    )
                except Exception:
                    pass
                return ApplyResult(
                    link_id=link_id,
                    success=False,
                    applied_parameters=None,
                    timestamp_ns=timestamp_ns,
                    error=f"{type(exc).__name__}: apply orchestration failed",
                )

        with self._lifecycle_lock:
            if not self._started or self._stopping or self._executor is not executor:
                raise RuntimeError("Toxiproxy plugin must be started before apply")
            results = executor.map(apply_one, selected)
        return tuple(results)

    def stop(self) -> None:
        with self._active_condition:
            if self._stopped:
                return
            if self._stopping:
                while not self._stopped:
                    self._active_condition.wait()
                return
            self._started = False
            self._stopping = True
            executor = self._executor
            self._executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)
        with self._active_condition:
            while self._active_applies:
                self._active_condition.wait()
        clear_toxics = bool(
            self._context is not None
            and self._context.config.controller.clear_toxics_on_exit
        )
        if clear_toxics:
            for link_id in self._managed_link_ids:
                try:
                    self._client.clear_dynamic_toxics(
                        self._links[link_id].proxy_name
                    )
                except Exception as exc:
                    self._logger.error(
                        "failed to clear dynamic toxics during shutdown",
                        extra={
                            "link_id": link_id,
                            "error_type": type(exc).__name__,
                        },
                    )
                    continue
        try:
            self._client.close()
        except Exception as exc:
            self._logger.error(
                "failed to close Toxiproxy client",
                extra={"error_type": type(exc).__name__},
            )
        finally:
            with self._active_condition:
                self._stopping = False
                self._stopped = True
                self._active_condition.notify_all()

    def health(self) -> dict[str, object]:
        with self._lifecycle_lock:
            stopped = self._stopped
            stopping = self._stopping
            started = self._started
            pending_initial_apply_count = len(self._pending_initial_apply_ids)
        if stopped:
            return {
                "status": "stopped",
                "managed_link_count": 0,
                "pending_initial_apply_count": pending_initial_apply_count,
            }
        if stopping:
            return {
                "status": "stopping",
                "managed_link_count": len(self._managed_link_ids),
                "pending_initial_apply_count": pending_initial_apply_count,
            }
        try:
            reachable = self._client.health_check(
                timeout_seconds=1.0,
                attempts=1,
            )
        except Exception:
            reachable = False
        if not reachable:
            status = "unhealthy"
        elif pending_initial_apply_count:
            status = "unhealthy"
        elif started:
            status = "ok"
        elif self._context is not None:
            status = "initialized"
        else:
            status = "created"
        return {
            "status": status,
            "managed_link_count": len(self._managed_link_ids),
            "pending_initial_apply_count": pending_initial_apply_count,
        }

    def _apply_and_store(
        self,
        link_id: str,
        generation: int,
        timestamp_ns: int,
    ) -> ApplyResult:
        if self._context is None:
            raise RuntimeError("Toxiproxy plugin is not initialized")
        try:
            link = self._links[link_id]
            link_lock = self._link_locks[link_id]
        except KeyError as exc:
            raise KeyError(f"unknown link_id: {link_id}") from exc
        with link_lock:
            snapshot = self._context.runtime_store.get_link(link_id)
            desired = snapshot.desired_parameters
            if desired is None:
                raise ValueError(f"link {link_id} has no desired parameters")
            previous_applied = (
                snapshot.applied_parameters
                if snapshot.last_apply_success
                else None
            )
            result = self._apply_with_self_healing(
                link,
                desired,
                previous_applied,
                timestamp_ns,
            )
            self._context.runtime_store.update_apply_result(
                link_id,
                success=result.success,
                applied_parameters=result.applied_parameters,
                timestamp_ns=timestamp_ns,
                generation=generation,
                error=result.error,
            )
            if result.success:
                with self._lifecycle_lock:
                    self._pending_initial_apply_ids.discard(link_id)
            return result

    def _apply_with_self_healing(
        self,
        link: ResolvedLinkConfig,
        desired: NetworkParameters,
        previous_applied: NetworkParameters | None,
        timestamp_ns: int,
    ) -> ApplyResult:
        """Apply desired state, reconciling a missing Toxiproxy proxy once.

        Toxiproxy 容器重启后 proxy/toxic 全部从内存丢失，而 Controller 不重启。
        这里对明确的 proxy-not-found（HTTP 404 / ProxyNotFoundError）做一次自愈：
        ensure_proxy 重建后再重新 apply 当前 desired state。最多 apply 两次，
        第二次仍失败立即按失败处理，禁止无限重试。

        仅当第一个 apply 抛出 ProxyNotFoundError 才触发自愈；ProxyConflict /
        连接不可用等其他异常不会被当作“缺失”误重建。
        """
        try:
            return self._applier.apply(
                link,
                desired,
                previous_applied,
                timestamp_ns,
            )
        except ProxyNotFoundError:
            pass
        except Exception as exc:
            return ApplyResult(
                link_id=link.link_id,
                success=False,
                applied_parameters=None,
                timestamp_ns=timestamp_ns,
                error=f"{type(exc).__name__}: Toxiproxy apply failed",
                packet_loss_applied=False,
            )

        self._logger.info(
            "reconciling missing toxiproxy proxy",
            extra={
                "link_id": link.link_id,
                "proxy_name": link.proxy_name,
            },
        )
        try:
            self._client.ensure_proxy(
                link.proxy_name,
                link.listen,
                link.upstream,
            )
        except Exception as exc:
            # 不吞 ProxyConflict / 连接不可用：不自动删除或覆盖其他 proxy，
            # 而是记录失败、last_apply_success=false、health 保持异常。
            return ApplyResult(
                link_id=link.link_id,
                success=False,
                applied_parameters=None,
                timestamp_ns=timestamp_ns,
                error=(
                    f"{type(exc).__name__}: failed to reconcile missing "
                    f"toxiproxy proxy {link.proxy_name}"
                ),
                packet_loss_applied=False,
            )
        # 自愈只重试一次；第二次仍失败（含再次 proxy-not-found）立即走失败处理。
        try:
            return self._applier.apply(
                link,
                desired,
                previous_applied,
                timestamp_ns,
            )
        except Exception as exc:
            return ApplyResult(
                link_id=link.link_id,
                success=False,
                applied_parameters=None,
                timestamp_ns=timestamp_ns,
                error=f"{type(exc).__name__}: Toxiproxy apply failed after proxy reconcile",
                packet_loss_applied=False,
            )

    @contextmanager
    def _active_apply(self) -> Iterator[None]:
        with self._active_condition:
            if not self._started or self._stopping:
                raise RuntimeError("Toxiproxy plugin must be started before apply")
            self._active_applies += 1
        try:
            yield
        finally:
            with self._active_condition:
                self._active_applies -= 1
                if self._active_applies == 0:
                    self._active_condition.notify_all()

    def _wait_until_healthy(self, timeout_seconds: float) -> None:
        assert self._context is not None
        shutdown_event = self._context.shutdown_event
        deadline = self._monotonic() + timeout_seconds
        while True:
            if shutdown_event.is_set():
                raise ToxiproxyUnavailableError(
                    "Toxiproxy startup interrupted by shutdown"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ToxiproxyUnavailableError(
                    "Toxiproxy did not become healthy within the startup window"
                )
            if self._client.health_check(
                timeout_seconds=remaining,
                attempts=1,
            ):
                return
            if shutdown_event.is_set():
                raise ToxiproxyUnavailableError(
                    "Toxiproxy startup interrupted by shutdown"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ToxiproxyUnavailableError(
                    "Toxiproxy did not become healthy within the startup window"
                )
            if shutdown_event.wait(min(0.25, remaining)):
                raise ToxiproxyUnavailableError(
                    "Toxiproxy startup interrupted by shutdown"
                )
