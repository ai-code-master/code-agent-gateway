"""Debounced provider health probing."""

import http.client
import threading
import time
import urllib.parse


class UpstreamHealth:
    def __init__(self, token_manager=None, upstream_base="", logger=None,
                 codex_probe=None):
        self._token_manager = token_manager
        self._upstream_base = upstream_base
        self._logger = logger
        self._lock = threading.Lock()
        self._healthy = True
        self._last_check = 0
        self._check_interval = 30
        self._consecutive_failures = 0
        self._failure_threshold = 2
        self._codex_probe = codex_probe
        self._codex_healthy = True

    def _do_probe(self):
        if not self._token_manager or not self._upstream_base:
            return "failure"
        try:
            token = self._token_manager.get_token()
            if not token:
                return "auth_failure"
            parsed = urllib.parse.urlparse(self._upstream_base)
            connection = http.client.HTTPSConnection(parsed.netloc, timeout=5)
            try:
                connection.request("GET", f"{parsed.path}/v1/models", headers={
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "KimiCLI/1.5",
                })
                status = connection.getresponse().status
            finally:
                connection.close()
            if status == 200:
                return "healthy"
            return "auth_failure" if status == 401 else "failure"
        except Exception as error:
            if self._logger:
                self._logger.debug("Upstream probe failed: %s", error)
            return "failure"

    def check(self):
        with self._lock:
            if time.time() - self._last_check < self._check_interval:
                return self._healthy and self._codex_healthy
        result = self._do_probe()
        codex_healthy = self._probe_codex()
        with self._lock:
            previous = self._healthy
            previous_codex = self._codex_healthy
            if result == "healthy":
                self._consecutive_failures = 0
                self._healthy = True
            elif result == "auth_failure":
                self._consecutive_failures = self._failure_threshold
                self._healthy = False
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._failure_threshold:
                    self._healthy = False
            self._last_check = time.time()
            self._codex_healthy = codex_healthy
            if self._logger and previous != self._healthy:
                log = self._logger.info if self._healthy else self._logger.warning
                log("Upstream health probe: %s", "healthy" if self._healthy else result)
            if self._logger and previous_codex != self._codex_healthy:
                log = self._logger.info if self._codex_healthy else self._logger.warning
                log(
                    "Codex health probe: %s",
                    "healthy" if self._codex_healthy else "failure",
                )
            return self._healthy and self._codex_healthy

    def is_healthy(self):
        with self._lock:
            if time.time() - self._last_check > self._check_interval * 3:
                return False
            return self._healthy and self._codex_healthy

    def is_ready(self):
        with self._lock:
            if time.time() - self._last_check > self._check_interval * 3:
                return False
            return self._healthy or self._codex_healthy

    def provider_status(self):
        with self._lock:
            return {
                "kimi": self._healthy,
                "codex": self._codex_healthy,
            }

    def _probe_codex(self):
        if not self._codex_probe:
            return True
        try:
            return bool(self._codex_probe())
        except Exception as error:
            if self._logger:
                self._logger.debug("Codex probe failed: %s", error)
            return False
