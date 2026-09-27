"""Typed configuration boundary for the gateway runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _bool(name: str, default: str) -> bool:
    return _env(name, default).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class GatewayConfig:
    """Configuration shared by extracted gateway modules.

    Legacy ``KCP_*`` names remain the public environment contract.
    """

    host: str
    port: int
    upstream_base: str
    auth_endpoint: str
    credentials_path: Path
    device_id_path: Path
    client_id: str
    max_concurrent: int
    rpm_limit: int
    upstream_timeout: int
    queue_timeout: int
    max_body_size: int
    debug_body: bool
    log_dir: Path
    log_dir_max_bytes: int
    enable_cache: bool
    cache_ttl: int
    enable_truncate: bool
    max_history_pairs: int
    graceful_shutdown_wait: int

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        home = Path.home()
        return cls(
            host=_env("KCP_HOST", "127.0.0.1"),
            port=int(_env("KCP_PORT", "8765")),
            upstream_base=_env("KCP_UPSTREAM_BASE", "https://api.kimi.com/coding"),
            auth_endpoint=_env("KCP_AUTH_ENDPOINT", "https://auth.kimi.com/api/oauth/token"),
            credentials_path=Path(_env("KCP_CREDENTIALS_PATH", str(home / ".kimi-code/credentials/kimi-code.json"))).expanduser(),
            device_id_path=Path(_env("KCP_DEVICE_ID_PATH", str(home / ".kimi-code/device_id"))).expanduser(),
            client_id=_env("KCP_CLIENT_ID", ""),
            max_concurrent=int(_env("KCP_MAX_CONCURRENT", "30")),
            rpm_limit=int(_env("KCP_RPM_LIMIT", "0")),
            upstream_timeout=int(_env("KCP_UPSTREAM_TIMEOUT", "600")),
            queue_timeout=int(_env("KCP_QUEUE_TIMEOUT", "300")),
            max_body_size=int(_env("KCP_MAX_BODY_SIZE", str(50 * 1024 * 1024))),
            debug_body=_bool("KCP_DEBUG_BODY", ""),
            log_dir=Path(_env("KCP_LOG_DIR", str(home / ".hermes/logs"))).expanduser(),
            log_dir_max_bytes=int(_env("KCP_LOG_DIR_MAX_BYTES", str(500 * 1024 * 1024))),
            enable_cache=_bool("KCP_ENABLE_CACHE", "1"),
            cache_ttl=int(_env("KCP_CACHE_TTL", "300")),
            enable_truncate=_bool("KCP_ENABLE_TRUNCATE", "1"),
            max_history_pairs=int(_env("KCP_MAX_HISTORY_PAIRS", "10")),
            graceful_shutdown_wait=int(_env("KCP_GRACEFUL_SHUTDOWN_WAIT", "30")),
        )

    def validate(self) -> None:
        if not self.client_id:
            raise ValueError("KCP_CLIENT_ID is required")
        if self.port < 1 or self.port > 65535:
            raise ValueError("KCP_PORT must be between 1 and 65535")
        if self.max_concurrent < 1:
            raise ValueError("KCP_MAX_CONCURRENT must be positive")
