"""Typed configuration boundary for the gateway runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"CAG_{name}", default)


def _bool(name: str, default: str) -> bool:
    return _env(name, default).lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class GatewayConfig:
    """Configuration shared by extracted gateway modules.

    ``CAG_*`` is the only supported environment contract.
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
    max_retries: int
    backoff_base: float
    refresh_interval: int
    refresh_threshold: int
    upstream_timeout: int
    queue_timeout: int
    max_body_size: int
    slow_request_threshold: float
    graceful_shutdown_wait: int
    debug_body: bool
    log_dir: Path
    log_max_bytes: int
    log_backup_count: int
    log_dir_max_bytes: int
    enable_cache: bool
    cache_ttl: int
    cache_max_entries: int
    enable_truncate: bool
    max_history_pairs: int
    max_assistant_chars: int
    enable_single_flight: bool
    single_flight_timeout: int
    enable_equivalence_cache: bool
    device_name: str
    device_model: str
    device_platform: str
    device_version: str
    os_version: str

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        home = Path.home()
        return cls(
            host=_env("HOST", "127.0.0.1"),
            port=int(_env("PORT", "8765")),
            upstream_base=_env("UPSTREAM_BASE", "https://api.kimi.com/coding"),
            auth_endpoint=_env("AUTH_ENDPOINT", "https://auth.kimi.com/api/oauth/token"),
            credentials_path=Path(_env("CREDENTIALS_PATH", str(home / ".kimi-code/credentials/kimi-code.json"))).expanduser(),
            device_id_path=Path(_env("DEVICE_ID_PATH", str(home / ".kimi-code/device_id"))).expanduser(),
            client_id=_env("CLIENT_ID", ""),
            max_concurrent=int(_env("MAX_CONCURRENT", "30")),
            rpm_limit=int(_env("RPM_LIMIT", "0")),
            max_retries=int(_env("MAX_RETRIES", "2")),
            backoff_base=float(_env("BACKOFF_BASE", "1.0")),
            refresh_interval=int(_env("REFRESH_INTERVAL", "300")),
            refresh_threshold=int(_env("REFRESH_THRESHOLD", "300")),
            upstream_timeout=int(_env("UPSTREAM_TIMEOUT", "600")),
            queue_timeout=int(_env("QUEUE_TIMEOUT", "300")),
            max_body_size=int(_env("MAX_BODY_SIZE", str(50 * 1024 * 1024))),
            slow_request_threshold=float(_env("SLOW_REQUEST_THRESHOLD", "30.0")),
            graceful_shutdown_wait=int(_env("GRACEFUL_SHUTDOWN_WAIT", "30")),
            debug_body=_bool("DEBUG_BODY", ""),
            log_dir=Path(_env("LOG_DIR", str(home / ".code-agent-gateway/logs"))).expanduser(),
            log_max_bytes=int(_env("LOG_MAX_BYTES", str(10 * 1024 * 1024))),
            log_backup_count=int(_env("LOG_BACKUP_COUNT", "3")),
            log_dir_max_bytes=int(_env("LOG_DIR_MAX_BYTES", str(500 * 1024 * 1024))),
            enable_cache=_bool("ENABLE_CACHE", "1"),
            cache_ttl=int(_env("CACHE_TTL", "300")),
            cache_max_entries=int(_env("CACHE_MAX_ENTRIES", "100")),
            enable_truncate=_bool("ENABLE_TRUNCATE", "1"),
            max_history_pairs=int(_env("MAX_HISTORY_PAIRS", "10")),
            max_assistant_chars=int(_env("MAX_ASSISTANT_CHARS", "2000")),
            enable_single_flight=_bool("ENABLE_SINGLE_FLIGHT", "1"),
            single_flight_timeout=int(_env("SINGLE_FLIGHT_TIMEOUT", "30")),
            enable_equivalence_cache=_bool("ENABLE_EQUIVALENCE_CACHE", "1"),
            device_name=_env("DEVICE_NAME", "CodeAgentGateway"),
            device_model=_env("DEVICE_MODEL", "Desktop"),
            device_platform=_env("DEVICE_PLATFORM", "macOS"),
            device_version=_env("DEVICE_VERSION", "2.1.153"),
            os_version=_env("OS_VERSION", ""),
        )

    def validate(self) -> None:
        if self.port < 1 or self.port > 65535:
            raise ValueError("CAG_PORT must be between 1 and 65535")
        if self.max_concurrent < 1:
            raise ValueError("CAG_MAX_CONCURRENT must be positive")

    def public(self) -> dict:
        return {
            "upstream_base": self.upstream_base,
            "max_concurrent": self.max_concurrent,
            "rpm_limit": self.rpm_limit,
            "upstream_timeout": self.upstream_timeout,
            "queue_timeout": self.queue_timeout,
            "max_body_size": self.max_body_size,
            "slow_request_threshold": self.slow_request_threshold,
            "debug_body": self.debug_body,
            "cache": {
                "enabled": self.enable_cache,
                "ttl": self.cache_ttl,
                "max_entries": self.cache_max_entries,
            },
            "truncation": {
                "enabled": self.enable_truncate,
                "max_history_pairs": self.max_history_pairs,
                "max_assistant_chars": self.max_assistant_chars,
            },
            "single_flight": {
                "enabled": self.enable_single_flight,
                "timeout": self.single_flight_timeout,
            },
            "equivalence_cache": {
                "enabled": self.enable_equivalence_cache,
                "mode": "normalized_equivalence",
            },
        }
