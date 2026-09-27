"""Low-level HTTP response behavior."""

import json


class ResponseMixin:
    logger = None
    metrics = None

    def log_message(self, format, *args):
        quiet = ("/healthz", "/metrics", "/admin/config", "/admin/reload")
        if self.path not in quiet:
            self.logger.info("%s %s -> %s", self.command, self.path, args[1])

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError) as error:
            client = self.client_address[0] if self.client_address else "unknown"
            self.logger.debug("Client %s disconnected early: %s", client, type(error).__name__)
            self.metrics.record_client_reset()
        except Exception as error:
            client = self.client_address[0] if self.client_address else "unknown"
            self.logger.error("Unhandled exception for client %s: %s", client, error, exc_info=True)

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "unknown"

    def _send_error(self, code, message, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

    def _send_response(self, status, headers, data):
        self.send_response(status)
        self._copy_headers(headers)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.logger.debug("Client disconnected during response write")

    def _send_stream_response(self, status, headers, response):
        self.send_response(status)
        self._copy_headers(headers)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        sent = 0
        try:
            while True:
                chunk = response.readline()
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            self.logger.debug("Client disconnected during streaming response")
            self.metrics.record_client_reset()
        finally:
            response.close()
            if hasattr(response, "_conn"):
                response._conn.close()
        return sent

    def _copy_headers(self, headers):
        blocked = {
            "connection", "transfer-encoding", "content-length",
            "date", "server", "set-cookie",
        }
        for header, value in headers:
            if header.lower() not in blocked:
                self.send_header(header, value)
