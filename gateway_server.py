#!/usr/bin/env python3
"""
Kimi Code OAuth Proxy v3.0

A local HTTP proxy that bridges OpenAI-compatible clients (like Hermes)
to the Kimi Code API with automatic OAuth token refresh.

Changelog v3.0:
- Single-flight request coalescing: concurrent identical requests share one upstream call
- Lightweight semantic cache: similar requests (not just identical) hit cache
- Token usage tracking: input/output/total tokens recorded per request

Changelog v2.9:
- Response cache: identical non-streaming requests return cached result (saves 100% token)
- Message truncation: auto-truncate long conversation history to reduce input tokens

Changelog v2.8:
- Dynamic max_tokens: smaller requests get smaller max_tokens
- Model list cache: avoids repeated upstream /v1/models queries
- Hot-reload .env via SIGHUP or /admin/reload endpoint
- Admin API for runtime config inspection
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from gateway.config import GatewayConfig
from gateway.cache import ModelListCache, ResponseCache, SingleFlight
from gateway.limits import RPMLimiter
from gateway.logging_setup import build_logger, install_exception_hook
from gateway.metrics import Metrics
from gateway.http import (
    ExecutionMixin, ProxyContext, ProxyMixin, ResponseMixin, RouteMixin,
)
from gateway.provider import TokenManager, UpstreamClient, UpstreamHealth
from gateway.request_body import RequestBodyProcessor
from gateway.runtime import runtime

# ==================== Configuration ====================
def _env(key, default=""):
    return os.environ.get(key, default)


_DOTENV_KEYS = set()


def _load_dotenv(path=".env", override=False):
    try:
        values = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    values[k] = v.strip()
        if override:
            for key in _DOTENV_KEYS - values.keys():
                os.environ.pop(key, None)
                _DOTENV_KEYS.discard(key)
        for k, value in values.items():
            if k in _DOTENV_KEYS or k not in os.environ:
                os.environ[k] = value
                _DOTENV_KEYS.add(k)
    except Exception:
        pass


def _reload_config():
    """Re-read .env and update runtime globals."""
    global CONFIG
    global MAX_CONCURRENT, RPM_LIMIT, MAX_RETRIES, BACKOFF_BASE
    global REFRESH_INTERVAL, REFRESH_THRESHOLD, UPSTREAM_TIMEOUT
    global QUEUE_TIMEOUT, MAX_BODY_SIZE, SLOW_REQUEST_THRESHOLD
    global GRACEFUL_SHUTDOWN_WAIT, DEBUG_BODY, LOG_DIR_MAX_BYTES

    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _load_dotenv(os.path.join(_script_dir, ".env"), override=True)
    CONFIG = GatewayConfig.from_env()

    MAX_CONCURRENT   = int(_env("KCP_MAX_CONCURRENT", "30"))
    RPM_LIMIT        = int(_env("KCP_RPM_LIMIT", "0"))
    MAX_RETRIES      = int(_env("KCP_MAX_RETRIES", "2"))
    BACKOFF_BASE     = float(_env("KCP_BACKOFF_BASE", "1.0"))
    REFRESH_INTERVAL = int(_env("KCP_REFRESH_INTERVAL", "300"))
    REFRESH_THRESHOLD = int(_env("KCP_REFRESH_THRESHOLD", "300"))
    UPSTREAM_TIMEOUT = int(_env("KCP_UPSTREAM_TIMEOUT", "600"))
    QUEUE_TIMEOUT    = int(_env("KCP_QUEUE_TIMEOUT", "300"))
    MAX_BODY_SIZE    = int(_env("KCP_MAX_BODY_SIZE", str(50 * 1024 * 1024)))
    SLOW_REQUEST_THRESHOLD = float(_env("KCP_SLOW_REQUEST_THRESHOLD", "30.0"))
    GRACEFUL_SHUTDOWN_WAIT = int(_env("KCP_GRACEFUL_SHUTDOWN_WAIT", "30"))
    DEBUG_BODY       = _env("KCP_DEBUG_BODY", "").lower() in ("1", "true", "yes")
    LOG_DIR_MAX_BYTES = int(_env("KCP_LOG_DIR_MAX_BYTES", str(500 * 1024 * 1024)))

    global ENABLE_CACHE, CACHE_TTL, CACHE_MAX_ENTRIES
    global ENABLE_TRUNCATE, MAX_HISTORY_PAIRS, MAX_ASSISTANT_CHARS
    global ENABLE_SINGLE_FLIGHT, SINGLE_FLIGHT_TIMEOUT
    global ENABLE_SEMANTIC_CACHE
    ENABLE_CACHE     = _env("KCP_ENABLE_CACHE", "1").lower() in ("1", "true", "yes")
    CACHE_TTL        = int(_env("KCP_CACHE_TTL", "300"))
    CACHE_MAX_ENTRIES = int(_env("KCP_CACHE_MAX_ENTRIES", "100"))
    ENABLE_TRUNCATE  = _env("KCP_ENABLE_TRUNCATE", "1").lower() in ("1", "true", "yes")
    MAX_HISTORY_PAIRS = int(_env("KCP_MAX_HISTORY_PAIRS", "10"))
    MAX_ASSISTANT_CHARS = int(_env("KCP_MAX_ASSISTANT_CHARS", "2000"))
    ENABLE_SINGLE_FLIGHT = _env("KCP_ENABLE_SINGLE_FLIGHT", "1").lower() in ("1", "true", "yes")
    SINGLE_FLIGHT_TIMEOUT = int(_env("KCP_SINGLE_FLIGHT_TIMEOUT", "30"))
    ENABLE_SEMANTIC_CACHE = _env("KCP_ENABLE_SEMANTIC_CACHE", "1").lower() in ("1", "true", "yes")
    logger.info("Config reloaded")


# Auto-load .env from the same directory as this script
_script_dir = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(_script_dir, ".env"))
CONFIG = GatewayConfig.from_env()

# Paths
CREDENTIALS_PATH = _env("KCP_CREDENTIALS_PATH", os.path.expanduser("~/.kimi-code/credentials/kimi-code.json"))
DEVICE_ID_PATH   = _env("KCP_DEVICE_ID_PATH",   os.path.expanduser("~/.kimi-code/device_id"))

# Endpoints
AUTH_ENDPOINT    = _env("KCP_AUTH_ENDPOINT",    "https://auth.kimi.com/api/oauth/token")
UPSTREAM_BASE    = _env("KCP_UPSTREAM_BASE",    "https://api.kimi.com/coding")
CLIENT_ID        = _env("KCP_CLIENT_ID",        "")

# Server
PROXY_HOST       = _env("KCP_HOST", "127.0.0.1")
PROXY_PORT       = int(_env("KCP_PORT", "8765"))
MAX_CONCURRENT   = int(_env("KCP_MAX_CONCURRENT", "30"))
RPM_LIMIT        = int(_env("KCP_RPM_LIMIT", "0"))
MAX_RETRIES      = int(_env("KCP_MAX_RETRIES", "2"))
BACKOFF_BASE     = float(_env("KCP_BACKOFF_BASE", "1.0"))
REFRESH_INTERVAL = int(_env("KCP_REFRESH_INTERVAL", "300"))
REFRESH_THRESHOLD = int(_env("KCP_REFRESH_THRESHOLD", "300"))
UPSTREAM_TIMEOUT = int(_env("KCP_UPSTREAM_TIMEOUT", "600"))
QUEUE_TIMEOUT    = int(_env("KCP_QUEUE_TIMEOUT", "300"))
MAX_BODY_SIZE    = int(_env("KCP_MAX_BODY_SIZE", str(50 * 1024 * 1024)))  # 50MB
SLOW_REQUEST_THRESHOLD = float(_env("KCP_SLOW_REQUEST_THRESHOLD", "30.0"))
GRACEFUL_SHUTDOWN_WAIT = int(_env("KCP_GRACEFUL_SHUTDOWN_WAIT", "30"))
DEBUG_BODY       = _env("KCP_DEBUG_BODY", "").lower() in ("1", "true", "yes")

# Cache & Truncation
ENABLE_CACHE     = _env("KCP_ENABLE_CACHE", "1").lower() in ("1", "true", "yes")
CACHE_TTL        = int(_env("KCP_CACHE_TTL", "300"))
CACHE_MAX_ENTRIES = int(_env("KCP_CACHE_MAX_ENTRIES", "100"))
ENABLE_TRUNCATE  = _env("KCP_ENABLE_TRUNCATE", "1").lower() in ("1", "true", "yes")
MAX_HISTORY_PAIRS = int(_env("KCP_MAX_HISTORY_PAIRS", "10"))
MAX_ASSISTANT_CHARS = int(_env("KCP_MAX_ASSISTANT_CHARS", "2000"))

# Single-flight & Semantic cache
ENABLE_SINGLE_FLIGHT = _env("KCP_ENABLE_SINGLE_FLIGHT", "1").lower() in ("1", "true", "yes")
SINGLE_FLIGHT_TIMEOUT = int(_env("KCP_SINGLE_FLIGHT_TIMEOUT", "30"))
ENABLE_SEMANTIC_CACHE = _env("KCP_ENABLE_SEMANTIC_CACHE", "1").lower() in ("1", "true", "yes")

# Logging
LOG_DIR          = _env("KCP_LOG_DIR", os.path.expanduser("~/.hermes/logs"))
LOG_FILE         = os.path.join(LOG_DIR, "kimi-proxy.log")
LOG_MAX_BYTES    = int(_env("KCP_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(_env("KCP_LOG_BACKUP_COUNT", "3"))
LOG_DIR_MAX_BYTES = int(_env("KCP_LOG_DIR_MAX_BYTES", str(500 * 1024 * 1024)))

# Device info (override to avoid leaking real machine names)
DEVICE_NAME      = _env("KCP_DEVICE_NAME",      "CodeAgentGateway")
DEVICE_MODEL     = _env("KCP_DEVICE_MODEL",     "Desktop")
DEVICE_PLATFORM  = _env("KCP_DEVICE_PLATFORM",  "macOS")
DEVICE_VERSION   = _env("KCP_DEVICE_VERSION",   "2.1.153")

# ==================== Validation ====================
if not CLIENT_ID:
    print("ERROR: KCP_CLIENT_ID is required. Set it in your .env file.", file=sys.stderr)
    sys.exit(1)

logger = build_logger(
    LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT, LOG_DIR_MAX_BYTES
)
install_exception_hook(logger)

metrics = Metrics(SLOW_REQUEST_THRESHOLD)

rpm_limiter = RPMLimiter(RPM_LIMIT)

# ==================== Concurrency Control ====================
kimi_semaphore = threading.Semaphore(MAX_CONCURRENT)
_shutdown_event = runtime.shutdown_event

model_cache = ModelListCache(UPSTREAM_BASE, logger, ttl=300)
response_cache = ResponseCache(
    ttl=CACHE_TTL,
    max_entries=CACHE_MAX_ENTRIES,
    enabled=lambda: ENABLE_CACHE,
    semantic_enabled=lambda: ENABLE_SEMANTIC_CACHE,
)
single_flight = SingleFlight(
    enabled=lambda: ENABLE_SINGLE_FLIGHT,
    timeout=lambda: SINGLE_FLIGHT_TIMEOUT,
)
def _upstream_probe_worker():
    check_interval = upstream_health._check_interval
    while not _shutdown_event.is_set():
        upstream_health.check()
        for _ in range(check_interval):
            if _shutdown_event.is_set():
                break
            time.sleep(1)


token_mgr = TokenManager(
    CREDENTIALS_PATH,
    DEVICE_ID_PATH,
    AUTH_ENDPOINT,
    CLIENT_ID,
    {
        "platform": DEVICE_PLATFORM,
        "version": DEVICE_VERSION,
        "device_name": DEVICE_NAME,
        "device_model": DEVICE_MODEL,
        "os_version": _env("KCP_OS_VERSION", ""),
    },
    logger,
    refresh_threshold=lambda: REFRESH_THRESHOLD,
)
upstream_health = UpstreamHealth(token_mgr, UPSTREAM_BASE, logger)

# ==================== Background Threads ====================
def refresh_worker():
    while not _shutdown_event.is_set():
        for _ in range(REFRESH_INTERVAL):
            if _shutdown_event.is_set():
                return
            time.sleep(1)
        if token_mgr.should_refresh():
            logger.info("Token expiring, refreshing...")
            token_mgr.refresh()


body_processor = RequestBodyProcessor(
    enabled=lambda: ENABLE_TRUNCATE,
    max_pairs=lambda: MAX_HISTORY_PAIRS,
    max_assistant_chars=lambda: MAX_ASSISTANT_CHARS,
    logger=logger,
)

upstream_client = UpstreamClient(
    timeout=lambda: UPSTREAM_TIMEOUT,
    max_retries=lambda: MAX_RETRIES,
    backoff_base=lambda: BACKOFF_BASE,
    logger=logger,
    metrics=metrics,
)
_do_kimi_request = upstream_client.request


# ==================== Admin Config ====================
def _current_config():
    return {
        "upstream_base": UPSTREAM_BASE,
        "max_concurrent": MAX_CONCURRENT,
        "rpm_limit": RPM_LIMIT,
        "upstream_timeout": UPSTREAM_TIMEOUT,
        "queue_timeout": QUEUE_TIMEOUT,
        "max_body_size": MAX_BODY_SIZE,
        "slow_request_threshold": SLOW_REQUEST_THRESHOLD,
        "debug_body": DEBUG_BODY,
        "cache": {
            "enabled": ENABLE_CACHE,
            "ttl": CACHE_TTL,
            "max_entries": CACHE_MAX_ENTRIES,
        },
        "truncation": {
            "enabled": ENABLE_TRUNCATE,
            "max_history_pairs": MAX_HISTORY_PAIRS,
            "max_assistant_chars": MAX_ASSISTANT_CHARS,
        },
        "single_flight": {
            "enabled": ENABLE_SINGLE_FLIGHT,
            "timeout": SINGLE_FLIGHT_TIMEOUT,
        },
        "semantic_cache": {
            "enabled": ENABLE_SEMANTIC_CACHE,
            "mode": "normalized_equivalence",
        },
    }


def _reload_http_config():
    global rpm_limiter, kimi_semaphore
    _reload_config()
    rpm_limiter = RPMLimiter(RPM_LIMIT)
    kimi_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT)
    response_cache.configure(CACHE_TTL, CACHE_MAX_ENTRIES)
    return _current_config()


proxy_context = ProxyContext(
    logger=logger,
    metrics=metrics,
    token_manager=token_mgr,
    model_cache=model_cache,
    response_cache=response_cache,
    single_flight=single_flight,
    body_processor=body_processor,
    upstream_client=upstream_client,
    runtime=runtime,
    config=_current_config,
    reload=_reload_http_config,
    rate_limiter=lambda: rpm_limiter,
    semaphore=lambda: kimi_semaphore,
)


# ==================== HTTP Proxy ====================
class ProxyHandler(
    RouteMixin, ProxyMixin, ExecutionMixin, ResponseMixin, BaseHTTPRequestHandler
):
    protocol_version = "HTTP/1.1"
    logger = logger
    metrics = metrics
    upstream_health = upstream_health
    token_manager = token_mgr
    response_cache = response_cache
    current_config = staticmethod(_current_config)
    active_requests = staticmethod(runtime.request_count)
    context = proxy_context

def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name
    if sig_name == "SIGHUP":
        logger.info("Received SIGHUP, reloading config...")
        _reload_config()
        global rpm_limiter
        rpm_limiter = RPMLimiter(RPM_LIMIT)
        return
    logger.info(f"Received {sig_name}, starting graceful shutdown...")
    _shutdown_event.set()


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    logger.info("Code Agent Gateway v3.0 started")
    logger.info(f"  Listen: http://{PROXY_HOST}:{PROXY_PORT}")
    logger.info(f"  Upstream: {UPSTREAM_BASE}")
    logger.info(f"  Concurrent: {MAX_CONCURRENT}, RPM limit: {RPM_LIMIT}, Upstream timeout: {UPSTREAM_TIMEOUT}s, Queue timeout: {QUEUE_TIMEOUT}s")
    logger.info(f"  Max body size: {MAX_BODY_SIZE} bytes")
    logger.info(f"  Slow request threshold: {SLOW_REQUEST_THRESHOLD}s")
    logger.info(f"  Log dir max: {LOG_DIR_MAX_BYTES} bytes")
    logger.info(f"  Graceful shutdown wait: {GRACEFUL_SHUTDOWN_WAIT}s")
    logger.info(f"  Debug body: {DEBUG_BODY}")
    logger.info(f"  Response cache: enabled={ENABLE_CACHE} semantic={ENABLE_SEMANTIC_CACHE} ttl={CACHE_TTL}s max_entries={CACHE_MAX_ENTRIES}")
    logger.info(f"  Message truncation: enabled={ENABLE_TRUNCATE} max_pairs={MAX_HISTORY_PAIRS} max_assistant_chars={MAX_ASSISTANT_CHARS}")
    logger.info(f"  Single-flight: enabled={ENABLE_SINGLE_FLIGHT} timeout={SINGLE_FLIGHT_TIMEOUT}s")

    # Start background threads
    threading.Thread(target=refresh_worker, daemon=True).start()
    threading.Thread(target=_upstream_probe_worker, daemon=True).start()

    # Run server in a thread so we can wait for shutdown signal
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()

    # Wait for shutdown signal
    _shutdown_event.wait()

    # Stop accepting new connections
    logger.info("Shutting down server...")
    server.shutdown()

    # Wait for active requests to complete
    wait_start = time.time()
    while runtime.request_count() > 0 and (time.time() - wait_start) < GRACEFUL_SHUTDOWN_WAIT:
        time.sleep(0.1)

    active_requests = runtime.request_count()
    if active_requests > 0:
        logger.warning(f"Force shutdown with {active_requests} active requests remaining")
    else:
        logger.info("All active requests completed, shutdown cleanly")

    server_thread.join(timeout=5)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
