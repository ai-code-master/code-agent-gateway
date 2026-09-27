"""Request-rate limiting primitives."""

import threading
import time


class RPMLimiter:
    """Thread-safe sliding-window requests-per-minute limiter."""

    def __init__(self, max_rpm: int):
        self._max_rpm = max_rpm
        self._lock = threading.Lock()
        self._timestamps = []

    def allow(self) -> bool:
        if self._max_rpm <= 0:
            return True
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._timestamps) >= self._max_rpm:
                return False
            self._timestamps.append(now)
            return True

    def current(self) -> int:
        with self._lock:
            self._prune(time.time())
            return len(self._timestamps)

    def _prune(self, now: float) -> None:
        cutoff = now - 60
        self._timestamps = [stamp for stamp in self._timestamps if stamp > cutoff]
