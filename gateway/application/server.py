"""Process lifecycle and graceful shutdown."""

import signal
import threading
import time
from http.server import ThreadingHTTPServer

from codex_bridge.appserver import shutdown_pool


def run(application):
    def handle_signal(signum, _frame):
        name = signal.Signals(signum).name
        if name == "SIGHUP":
            application.logger.info("Received SIGHUP, reloading config...")
            application.reload()
            return
        application.logger.info("Received %s, starting graceful shutdown...", name)
        application.runtime.shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)
    config = application.settings.current
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((config.host, config.port), application.handler)
    _log_startup(application)
    application.start_workers()
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    application.runtime.shutdown_event.wait()
    application.logger.info("Shutting down server...")
    server.shutdown()
    deadline = time.time() + application.settings.current.graceful_shutdown_wait
    while application.runtime.request_count() > 0 and time.time() < deadline:
        time.sleep(0.1)
    active = application.runtime.request_count()
    if active:
        application.logger.warning("Force shutdown with %s active requests", active)
    else:
        application.logger.info("All active requests completed, shutdown cleanly")
    server_thread.join(timeout=5)
    shutdown_pool()
    application.logger.info("Shutdown complete")


def _log_startup(application):
    config = application.settings.current
    log = application.logger.info
    log("Code Agent Gateway v3.1 started")
    log("  Purpose: expose local AI coding subscriptions as OpenAI-compatible APIs")
    log("  Listen: http://%s:%s", config.host, config.port)
    log("  Upstream: %s", config.upstream_base)
    log("  Concurrent: %s, RPM limit: %s", config.max_concurrent, config.rpm_limit)
    log("  Upstream timeout: %ss, Queue timeout: %ss", config.upstream_timeout, config.queue_timeout)
    log("  Response cache: enabled=%s ttl=%ss", config.enable_cache, config.cache_ttl)
    log("  Single-flight: enabled=%s timeout=%ss", config.enable_single_flight, config.single_flight_timeout)
