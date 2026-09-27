"""Explicit dependencies shared by HTTP request mixins."""

from dataclasses import dataclass


@dataclass
class ProxyContext:
    logger: object
    metrics: object
    token_manager: object
    model_cache: object
    response_cache: object
    single_flight: object
    body_processor: object
    upstream_client: object
    codex_provider: object
    runtime: object
    config: object
    reload: object
    rate_limiter: object
    semaphore: object


@dataclass
class PreparedRequest:
    method: str
    path: str
    target_url: str
    body: bytes
    body_json: dict
    body_length: int
    model: str
    streaming: bool
    headers: dict
    started_at: float
    client_ip: str
    request_id: str
