# Code Agent Gateway

Turn AI coding subscriptions already signed in on your computer into one
OpenAI-compatible local API.

The gateway reuses local Kimi Code and Codex sessions. Clients such as Hermes
only need one endpoint:

```text
Hermes / OpenAI-compatible client
                 |
                 v
http://127.0.0.1:8765/v1
                 |
          +------+------+
          |             |
     Kimi Code       Codex CLI
     OAuth session   login session
```

## Scope

This project is a local coding-subscription adapter, not an enterprise AI
control plane. Its priorities are:

- discover locally installed coding agents and their models;
- reuse local login sessions without copying short-lived access tokens;
- translate provider protocols to OpenAI-compatible APIs;
- keep provider processes healthy and reusable;
- apply bounded queues, retries, and circuit breaking under load;
- expose everything through one localhost-only port.

It intentionally does not provide cloud accounts, billing, organization
management, or LAN access.

## Supported APIs

| Endpoint | Kimi Code | Codex |
|---|---:|---:|
| `GET /v1/models` | Yes | Yes |
| `POST /v1/chat/completions` | Yes | Yes |
| `POST /v1/responses` | No | Yes |
| Streaming | Yes | Yes |
| Function tools | Pass-through | Translated |

The requested model selects the provider automatically. Codex models are
available as stable aliases such as `codex-sol` and as discovered
`codex/<model-id>` names.

## Quick start

```bash
git clone https://github.com/ai-code-master/code-agent-gateway.git
cd code-agent-gateway
cp .env.example .env
# Set CAG_CLIENT_ID when using Kimi Code
./start.sh
```

Kimi credentials are read from
`~/.kimi-code/credentials/kimi-code.json`. Codex is discovered from `PATH`
and uses the current `codex login` session. Override the executable with
`CODEX_BIN=/absolute/path/to/codex` when needed.

## Hermes

Hermes speaks the OpenAI API but does not directly manage the Kimi Code OAuth
session. Point it at the gateway:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8765/v1
export OPENAI_API_KEY=local-gateway
```

For Hermes installations using Kimi-specific variable names:

```bash
KIMI_BASE_URL=http://127.0.0.1:8765
KIMI_API_KEY=local-gateway
```

The API key value is ignored because the service only listens on localhost.

## Configuration

Configuration is loaded from `.env` or the process environment.

| Variable | Default | Purpose |
|---|---|---|
| `CAG_CLIENT_ID` | empty | Kimi Code OAuth refresh client ID |
| `CAG_CREDENTIALS_PATH` | Kimi CLI credentials | Kimi login session |
| `CAG_HOST` | `127.0.0.1` | Listen address |
| `CAG_PORT` | `8765` | Unified port |
| `CAG_MAX_CONCURRENT` | `30` | Request concurrency |
| `CAG_HTTP_WORKERS` | `64` | Bounded HTTP worker threads |
| `CAG_HTTP_PENDING` | `128` | Pending HTTP requests before 503 |
| `CAG_CODEX_POOL_SIZE` | `2` | Reusable Codex processes |
| `CAG_CODEX_QUEUE_TIMEOUT` | `30` | Maximum Codex pool wait |
| `CAG_CODEX_CWD` | user home | Default Codex working directory |
| `CAG_LOG_DIR` | `~/.code-agent-gateway/logs` | Log directory |

See [`.env.example`](.env.example) for cache, timeout, logging, and device
settings.

## macOS service

```bash
./launchd/install.sh
launchctl print gui/$(id -u)/io.github.code-agent-gateway
```

Only the unified `8765` service is installed. Codex App Server child
processes are owned and reused by the gateway.

## Health and diagnostics

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/v1/models
curl http://127.0.0.1:8765/metrics
```

`/healthz` reports Kimi and Codex separately, including Codex process-pool
state, queue wait statistics, Kimi connection-pool state, and circuit status.

Response caching is conservative: only non-streaming requests with
`temperature: 0` and no tools are cached, and the complete request payload
participates in the cache key.

## Architecture

`gateway_server.py` is a thin entrypoint. Runtime composition, HTTP
delivery, protocol adapters, provider integrations, and caches are separated
under `gateway/`; the Codex App Server transport lives under
`codex_bridge/`.

The project has no third-party Python runtime dependency. Source files are
kept below 200 lines and folders below eight files.

Runtime reload applies limits, cache settings, and thresholds. Listen
addresses, credentials, upstream endpoints, and HTTP worker sizing require a
service restart; `/admin/reload` reports such changes explicitly.

## Security

The gateway deliberately binds to `127.0.0.1`. Do not expose it on a LAN or
the public internet: it reuses authenticated local subscriptions and does not
implement inbound user authentication.

## License

MIT
