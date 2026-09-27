"""Short-lived cache for provider model metadata."""

from __future__ import annotations

import http.client
import threading
import time
import urllib.parse


class ModelListCache:
    def __init__(self, upstream_base: str, logger, ttl: int = 300):
        self._upstream_base = upstream_base
        self._logger = logger
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data = None
        self._expires_at = 0
        self._refreshing = False

    def get(self, token: str) -> bytes | None:
        with self._lock:
            if self._data and time.time() < self._expires_at:
                return self._data
            stale = self._data
            if not self._refreshing and token:
                self._refreshing = True
                threading.Thread(
                    target=self._refresh, args=(token,), daemon=True,
                    name="kimi-model-refresh",
                ).start()
            return stale

    def warm(self, token):
        self.get(token)

    def status(self):
        with self._lock:
            return {
                "cached": self._data is not None,
                "refreshing": self._refreshing,
                "expires_in": max(0, self._expires_at - time.time()),
            }

    def _refresh(self, token):
        data = None
        try:
            parsed = urllib.parse.urlparse(self._upstream_base)
            connection = http.client.HTTPSConnection(parsed.netloc, timeout=10)
            try:
                connection.request(
                    "GET",
                    f"{parsed.path}/v1/models",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "KimiCLI/1.5",
                    },
                )
                response = connection.getresponse()
                payload = response.read()
                data = payload if response.status == 200 else None
            finally:
                connection.close()
        except Exception as error:
            self._logger.debug("Model list fetch failed: %s", error)
        finally:
            with self._lock:
                if data:
                    self._data = data
                    self._expires_at = time.time() + self._ttl
                else:
                    self._expires_at = time.time() + min(30, self._ttl)
                self._refreshing = False
