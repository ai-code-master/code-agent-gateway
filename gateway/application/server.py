"""Process lifecycle and graceful shutdown."""

import signal
import time

from codex_bridge.appserver import shutdown_pool
from .http_server import BoundedHTTPServer


def run(application):
    def handle_signal(signum, _frame):
        name = signal.Signals(signum).name
        if name == "SIGHUP":
            application.logger.info("Received SIGHUP, reloading config...")
            application.reload()
            return
        application.logger.info("Received %s, starting graceful shutdown...", name)
        application.admission.close()
        application.runtime.shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)
    config = application.settings.current
    server = BoundedHTTPServer(
        (config.host, config.port), application.handler,
        max_workers=config.http_workers,
        max_pending=config.http_pending,
        backlog=config.http_backlog,
    )
    _log_startup(application)
    application.start_workers()
    server.timeout = 0.5
    while not application.runtime.shutdown_event.is_set():
        server.handle_request()
    application.logger.info("Shutting down server...")
    deadline = time.time() + application.settings.current.graceful_shutdown_wait
    while application.runtime.request_count() > 0 and time.time() < deadline:
        time.sleep(0.1)
    active = application.runtime.request_count()
    if active:
        application.logger.warning("Force shutdown with %s active requests", active)
    else:
        application.logger.info("All active requests completed, shutdown cleanly")
    shutdown_pool()
    application.upstream_client.close()
    server.server_close()
    application.logger.info("Shutdown complete")


def _log_startup(application):
    config = application.settings.current
    log = application.logger.info
    log("Code Agent Gateway v3.2 started")
    log("  Purpose: expose local AI coding subscriptions as OpenAI-compatible APIs")
    log("  Listen: http://%s:%s", config.host, config.port)
    log("  Upstream: %s", config.upstream_base)
    log("  Concurrent: %s, RPM limit: %s", config.max_concurrent, config.rpm_limit)
    log(
        "  HTTP workers: %s, pending: %s, backlog: %s",
        config.http_workers, config.http_pending, config.http_backlog,
    )
    log("  Upstream timeout: %ss, Queue timeout: %ss", config.upstream_timeout, config.queue_timeout)
    log("  Response cache: enabled=%s ttl=%ss", config.enable_cache, config.cache_ttl)
    log("  Single-flight: enabled=%s timeout=%ss", config.enable_single_flight, config.single_flight_timeout)
