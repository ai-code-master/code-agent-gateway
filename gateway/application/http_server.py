"""Bounded HTTP worker pool with explicit overload responses."""

import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer


class BoundedHTTPServer(HTTPServer):
    allow_reuse_address = True

    def __init__(
        self, address, handler, max_workers=64, max_pending=128, backlog=128
    ):
        self.request_queue_size = backlog
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="gateway-http"
        )
        self._slots = threading.BoundedSemaphore(max_workers + max_pending)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self._reject(request)
            return
        try:
            self._executor.submit(self._handle, request, client_address)
        except Exception:
            self._slots.release()
            self.shutdown_request(request)
            raise

    def _handle(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)
            self._slots.release()

    def _reject(self, request):
        payload = b'{"error":"Gateway HTTP worker queue is full"}'
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json\r\n"
            b"Retry-After: 1\r\n"
            b"Connection: close\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode()
            + payload
        )
        try:
            request.sendall(response)
        except (OSError, socket.error):
            pass
        finally:
            self.shutdown_request(request)

    def server_close(self):
        super().server_close()
        self._executor.shutdown(wait=True, cancel_futures=True)
