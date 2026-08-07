"""Small synchronous event bus with subscriber failure isolation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
import logging
from threading import RLock
from typing import TypeVar


EventT = TypeVar("EventT")
EventHandler = Callable[[object], None]


class EventBus:
    """Publish immutable events to handlers in subscription order."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._handlers: dict[type[object], list[EventHandler]] = defaultdict(list)
        self._lock = RLock()
        self._logger = logger or logging.getLogger(__name__)

    def subscribe(
        self,
        event_type: type[EventT],
        handler: Callable[[EventT], None],
    ) -> None:
        with self._lock:
            handlers = self._handlers[event_type]
            if handler not in handlers:
                handlers.append(handler)  # type: ignore[arg-type]

    def unsubscribe(
        self,
        event_type: type[EventT],
        handler: Callable[[EventT], None],
    ) -> None:
        with self._lock:
            handlers = self._handlers.get(event_type)
            if not handlers:
                return
            try:
                handlers.remove(handler)  # type: ignore[arg-type]
            except ValueError:
                return
            if not handlers:
                self._handlers.pop(event_type, None)

    def publish(self, event: object) -> int:
        """Publish an event and return the number of failed handlers."""

        with self._lock:
            handlers = tuple(self._handlers.get(type(event), ()))

        failure_count = 0
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                failure_count += 1
                self._logger.error(
                    "event handler failed",
                    extra={
                        "event_type": type(event).__name__,
                        "handler": getattr(handler, "__qualname__", type(handler).__name__),
                        "error_type": type(exc).__name__,
                    },
                )
        return failure_count
