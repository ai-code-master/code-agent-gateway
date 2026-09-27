"""Thread-safe reusable HTTPS connections for provider traffic."""

import http.client
import os
import queue
import threading
import urllib.parse


class HTTPSConnectionPool:
    def __init__(self, timeout, max_idle):
        self._timeout = timeout
        self._max_idle = max_idle
        self._lock = threading.Lock()
        self._pools = {}
        self._active = set()
        self._closed = False
        self._created = 0
        self._reused = 0

    def borrow(self, host):
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        key = (host, proxy_url or "")
        with self._lock:
            if self._closed:
                raise RuntimeError("Upstream client is closed")
            pool = self._pools.get(key)
            if pool is None:
                pool = queue.LifoQueue(maxsize=max(1, self._max_idle()))
                self._pools[key] = pool
            try:
                connection = pool.get_nowait()
                self._reused += 1
            except queue.Empty:
                connection = self._create(host, proxy_url)
                self._created += 1
            self._active.add(connection)
        return connection, key

    def release(self, connection, key, reusable):
        with self._lock:
            self._active.discard(connection)
            pool = self._pools.get(key)
            if reusable and not self._closed and pool is not None:
                try:
                    pool.put_nowait(connection)
                    return
                except queue.Full:
                    pass
        connection.close()

    def close(self):
        with self._lock:
            self._closed = True
            active = list(self._active)
            pools, self._pools = self._pools, {}
        for connection in active:
            connection.close()
        for pool in pools.values():
            while True:
                try:
                    pool.get_nowait().close()
                except queue.Empty:
                    break

    def status(self):
        with self._lock:
            return {
                "active_connections": len(self._active),
                "idle_connections": sum(
                    pool.qsize() for pool in self._pools.values()
                ),
                "connections_created": self._created,
                "connections_reused": self._reused,
            }

    def _create(self, host, proxy_url):
        if not proxy_url:
            return http.client.HTTPSConnection(host, timeout=self._timeout())
        proxy = urllib.parse.urlparse(proxy_url)
        connection = http.client.HTTPSConnection(
            proxy.hostname, proxy.port or 7897, timeout=self._timeout()
        )
        connection.set_tunnel(host)
        return connection
