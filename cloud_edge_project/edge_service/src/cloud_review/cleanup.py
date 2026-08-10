"""Periodic removal of expired edge-owned cloud-review packets."""

from __future__ import annotations

import threading

from .store import CloudReviewStore


class CloudReviewCleanupWorker:
    def __init__(
        self,
        store: CloudReviewStore,
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.store = store
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="edge-cloud-review-cleanup",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(self.interval_seconds * 2, 1.0))

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.store.cleanup_expired()
            self._stop_event.wait(self.interval_seconds)
