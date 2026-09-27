"""Pooled HTTPS transport with bounded retries and circuit breaking."""

import random
import threading
import time
import urllib.parse

from .connections import HTTPSConnectionPool


class CircuitOpenError(RuntimeError):
    pass


class UpstreamClient:
    RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self, timeout, max_retries, backoff_base, logger, metrics,
        max_idle=lambda: 8, circuit_threshold=lambda: 5,
        circuit_cooldown=lambda: 30,
    ):
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._circuit_threshold = circuit_threshold
        self._circuit_cooldown = circuit_cooldown
        self._logger = logger
        self._metrics = metrics
        self._connections = HTTPSConnectionPool(timeout, max_idle)
        self._breaker_lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0

    def request(self, method, target_url, body, headers, retries=0):
        parsed = urllib.parse.urlparse(target_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        last_error = None
        for attempt in range(retries, self._max_retries() + 1):
            if self._circuit_is_open():
                return None, CircuitOpenError("Kimi upstream circuit is open")
            connection, pool_key = self._connections.borrow(parsed.netloc)
            try:
                request_headers = {**headers, "Connection": "keep-alive"}
                connection.request(
                    method, path, body=body, headers=request_headers
                )
                response = connection.getresponse()
                self._attach(response, connection, pool_key)
                if (
                    response.status in self.RETRYABLE_STATUS
                    and attempt < self._max_retries()
                ):
                    wait = self._retry_delay(response, attempt)
                    self._metrics.record_retry()
                    self._logger.warning(
                        "HTTP %s, retry in %.2fs (attempt %s)",
                        response.status, wait, attempt + 1,
                    )
                    self.release(response, reusable=False)
                    if response.status != 429:
                        self._record_failure()
                    time.sleep(wait)
                    continue
                if response.status >= 500:
                    self._record_failure()
                elif response.status != 429:
                    self._record_success()
                return response, None
            except Exception as error:
                last_error = error
                self._connections.release(
                    connection, pool_key, reusable=False
                )
                self._record_failure()
                if attempt >= self._max_retries():
                    break
                wait = self._backoff(attempt)
                self._metrics.record_retry()
                self._logger.warning(
                    "%s, retry in %.2fs (attempt %s)",
                    type(error).__name__, wait, attempt + 1,
                )
                time.sleep(wait)
        return None, last_error

    def release(self, response, reusable=True):
        connection = getattr(response, "_cag_connection", None)
        pool_key = getattr(response, "_cag_pool_key", None)
        if not connection:
            response.close()
            return
        reusable = reusable and not response.will_close
        try:
            response.close()
        finally:
            self._connections.release(connection, pool_key, reusable)

    def close(self):
        self._connections.close()

    def status(self):
        status = self._connections.status()
        with self._breaker_lock:
            status.update({
                "circuit_failures": self._failures,
                "circuit_open": time.monotonic() < self._open_until,
            })
        return status

    @staticmethod
    def _attach(response, connection, pool_key):
        response._cag_connection = connection
        response._cag_pool_key = pool_key

    def _retry_delay(self, response, attempt):
        retry_after = response.getheader("Retry-After")
        try:
            if retry_after is not None:
                return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
        return self._backoff(attempt)

    def _backoff(self, attempt):
        base = self._backoff_base() * (2 ** attempt)
        return base * random.uniform(0.75, 1.25)

    def _record_success(self):
        with self._breaker_lock:
            self._failures = 0
            self._open_until = 0.0

    def _record_failure(self):
        with self._breaker_lock:
            self._failures += 1
            if self._failures >= self._circuit_threshold():
                self._open_until = time.monotonic() + self._circuit_cooldown()

    def _circuit_is_open(self):
        with self._breaker_lock:
            if time.monotonic() >= self._open_until:
                return False
            return True
