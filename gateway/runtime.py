"""Shared runtime state used while the legacy entrypoint is being migrated."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    """Process-wide mutable state with explicit ownership."""

    shutdown_event: threading.Event = field(default_factory=threading.Event)
    active_requests: int = 0
    active_lock: threading.Lock = field(default_factory=threading.Lock)

    def begin_request(self) -> int:
        with self.active_lock:
            self.active_requests += 1
            return self.active_requests

    def end_request(self) -> int:
        with self.active_lock:
            self.active_requests = max(0, self.active_requests - 1)
            return self.active_requests

    def request_count(self) -> int:
        with self.active_lock:
            return self.active_requests


runtime = RuntimeState()
