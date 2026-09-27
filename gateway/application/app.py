"""Composition root for gateway services."""

import threading

from gateway.cache import ModelListCache, ResponseCache, SingleFlight
from gateway.http import ProxyContext
from gateway.limits import RPMLimiter
from gateway.logging_setup import build_logger, install_exception_hook
from gateway.metrics import Metrics
from gateway.provider import CodexProvider, TokenManager, UpstreamClient, UpstreamHealth
from gateway.request_body import RequestBodyProcessor
from gateway.runtime import runtime

from .handler import create_handler
from .admission import AdmissionController
from .server import run
from .settings import SettingsStore


class GatewayApplication:
    def __init__(self, base_dir):
        self.settings = SettingsStore(base_dir)
        config = self.settings.current
        log_file = config.log_dir / "code-agent-gateway.log"
        self.logger = build_logger(
            str(config.log_dir), str(log_file), config.log_max_bytes,
            config.log_backup_count, config.log_dir_max_bytes,
        )
        install_exception_hook(self.logger)
        self.runtime = runtime
        self.metrics = Metrics(config.slow_request_threshold)
        self.rate_limiter = RPMLimiter(config.rpm_limit)
        self.admission = AdmissionController(self.runtime, config.max_concurrent)
        self.model_cache = ModelListCache(config.upstream_base, self.logger)
        self.response_cache = ResponseCache(
            ttl=config.cache_ttl,
            max_entries=config.cache_max_entries,
            enabled=lambda: self.settings.current.enable_cache,
            equivalence_enabled=lambda: self.settings.current.enable_equivalence_cache,
        )
        self.single_flight = SingleFlight(
            enabled=lambda: self.settings.current.enable_single_flight,
            timeout=lambda: self.settings.current.single_flight_timeout,
        )
        self.token_manager = self._token_manager()
        self.codex_provider = CodexProvider(
            self.logger,
            queue_timeout=lambda: self.settings.current.codex_queue_timeout,
            request_timeout=lambda: self.settings.current.upstream_timeout,
        )
        self.upstream_health = UpstreamHealth(
            self.token_manager, config.upstream_base, self.logger,
            codex_probe=self.codex_provider.health,
        )
        self.body_processor = RequestBodyProcessor(
            enabled=lambda: self.settings.current.enable_truncate,
            max_pairs=lambda: self.settings.current.max_history_pairs,
            max_assistant_chars=lambda: self.settings.current.max_assistant_chars,
            logger=self.logger,
        )
        self.upstream_client = UpstreamClient(
            timeout=lambda: self.settings.current.upstream_timeout,
            max_retries=lambda: self.settings.current.max_retries,
            backoff_base=lambda: self.settings.current.backoff_base,
            max_idle=lambda: self.settings.current.upstream_idle_connections,
            circuit_threshold=lambda: self.settings.current.circuit_failure_threshold,
            circuit_cooldown=lambda: self.settings.current.circuit_cooldown,
            logger=self.logger,
            metrics=self.metrics,
        )
        self.proxy_context = ProxyContext(
            logger=self.logger,
            metrics=self.metrics,
            token_manager=self.token_manager,
            model_cache=self.model_cache,
            response_cache=self.response_cache,
            single_flight=self.single_flight,
            body_processor=self.body_processor,
            upstream_client=self.upstream_client,
            codex_provider=self.codex_provider,
            admission=self.admission,
            config=self.public_config,
            reload=self.reload,
            rate_limiter=lambda: self.rate_limiter,
        )
        self.handler = create_handler(self)

    def _token_manager(self):
        config = self.settings.current
        return TokenManager(
            str(config.credentials_path), str(config.device_id_path),
            config.auth_endpoint, config.client_id,
            {
                "platform": config.device_platform,
                "version": config.device_version,
                "device_name": config.device_name,
                "device_model": config.device_model,
                "os_version": config.os_version,
            },
            self.logger,
            refresh_threshold=lambda: self.settings.current.refresh_threshold,
        )

    def public_config(self):
        return self.settings.current.public()

    def reload(self):
        previous = self.settings.current
        config = self.settings.reload()
        self.rate_limiter = RPMLimiter(config.rpm_limit)
        self.admission.configure(config.max_concurrent)
        self.response_cache.configure(config.cache_ttl, config.cache_max_entries)
        self.metrics.slow_threshold = config.slow_request_threshold
        static_fields = (
            "host", "port", "upstream_base", "auth_endpoint",
            "credentials_path", "device_id_path", "client_id",
            "device_platform", "device_version", "device_name",
            "device_model", "os_version",
            "http_workers", "http_pending", "http_backlog",
        )
        restart_required = [
            name for name in static_fields
            if getattr(previous, name) != getattr(config, name)
        ]
        if restart_required:
            self.logger.warning(
                "Restart required for config changes: %s",
                ", ".join(restart_required),
            )
        self.logger.info("Config reloaded")
        result = config.public()
        result["restart_required_changes"] = restart_required
        return result

    def start_workers(self):
        self.model_cache.warm(self.token_manager.get_token())
        self.codex_provider.warm_models()
        threading.Thread(target=self._refresh_worker, daemon=True).start()
        threading.Thread(target=self._health_worker, daemon=True).start()

    def _refresh_worker(self):
        while not self.runtime.shutdown_event.is_set():
            config = self.settings.current
            interval = min(
                config.refresh_interval,
                max(15, config.refresh_threshold // 2),
            )
            if self.runtime.shutdown_event.wait(interval):
                break
            if self.token_manager.should_refresh():
                self.logger.info("Token expiring, refreshing...")
                self.token_manager.refresh()

    def _health_worker(self):
        interval = self.upstream_health._check_interval
        while not self.runtime.shutdown_event.is_set():
            self.upstream_health.check()
            self.runtime.shutdown_event.wait(interval)

    def run(self):
        run(self)
