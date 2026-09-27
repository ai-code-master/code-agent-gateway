"""HTTP handler composition without reverse imports."""

from http.server import BaseHTTPRequestHandler

from gateway.http import (
    CodexMixin, ExecutionMixin, ProxyMixin, ResponseMixin, ResponsesMixin,
    RouteMixin,
)


def create_handler(application):
    class GatewayHandler(
        RouteMixin, ProxyMixin, ResponsesMixin, CodexMixin, ExecutionMixin,
        ResponseMixin,
        BaseHTTPRequestHandler,
    ):
        protocol_version = "HTTP/1.1"
        logger = application.logger
        metrics = application.metrics
        upstream_health = application.upstream_health
        codex_provider = application.codex_provider
        token_manager = application.token_manager
        response_cache = application.response_cache
        current_config = staticmethod(application.public_config)
        active_requests = staticmethod(application.runtime.request_count)
        context = application.proxy_context

    GatewayHandler.__name__ = "ProxyHandler"
    return GatewayHandler
