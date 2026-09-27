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

    def get(self, token: str) -> bytes | None:
        with self._lock:
            if self._data and time.time() < self._expires_at:
                return self._data
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
                data = response.read()
                if response.status == 200:
                    with self._lock:
                        self._data = data
                        self._expires_at = time.time() + self._ttl
                    return data
            finally:
                connection.close()
        except Exception as error:
            self._logger.debug("Model list fetch failed: %s", error)
        return None
