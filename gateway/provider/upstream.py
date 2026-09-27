"""HTTPS transport with bounded retries."""

import http.client
import os
import time
import urllib.parse


class UpstreamClient:
    def __init__(self, timeout, max_retries, backoff_base, logger, metrics):
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._logger = logger
        self._metrics = metrics

    def request(self, method, target_url, body, headers, retries=0):
        parsed = urllib.parse.urlparse(target_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        connection = self._connection(parsed.netloc)
        try:
            request_headers = {**headers, "Connection": "close"}
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            if response.status in (429, 502, 503) and retries < self._max_retries():
                wait = self._backoff_base() * (2 ** retries)
                self._logger.warning(
                    "HTTP %s, retry in %.1fs (attempt %s)",
                    response.status, wait, retries + 1,
                )
                self._metrics.record_retry()
                response.close()
                connection.close()
                time.sleep(wait)
                return self.request(method, target_url, body, headers, retries + 1)
            response._conn = connection
            return response, None
        except Exception as error:
            connection.close()
            return None, error

    def _connection(self, host):
        proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if not proxy_url:
            return http.client.HTTPSConnection(host, timeout=self._timeout())
        proxy = urllib.parse.urlparse(proxy_url)
        connection = http.client.HTTPSConnection(
            proxy.hostname, proxy.port or 7897, timeout=self._timeout()
        )
        connection.set_tunnel(host)
        return connection
