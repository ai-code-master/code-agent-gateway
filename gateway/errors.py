"""Stable error categories returned by the proxy."""

import socket
import ssl


def classify_error(error):
    if type(error).__name__ == "CircuitOpenError":
        return "upstream_circuit_open", str(error)
    if isinstance(error, socket.timeout):
        return "upstream_timeout", "Upstream read timed out (model may be thinking too long)"
    if isinstance(error, ConnectionResetError):
        return "connection_reset", "Upstream closed connection unexpectedly"
    if isinstance(error, BrokenPipeError):
        return "broken_pipe", "Connection broken while sending request"
    if isinstance(error, ssl.SSLError):
        return "ssl_error", f"TLS/SSL error: {error}"
    if isinstance(error, OSError) and error.errno in (61, 111, 51, 8):
        return "connection_refused", "Cannot connect to upstream (network or DNS issue)"
    return type(error).__name__.lower(), str(error)
