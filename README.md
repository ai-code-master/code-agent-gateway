# Code Agent Gateway

A lightweight local gateway that bridges coding agents and other **OpenAI-compatible clients** to multiple AI backends.

The repository also includes a **Codex App Server bridge** for connecting WorkBuddy to a locally authenticated Codex subscription. The two backends run independently:

- Kimi-compatible backend: `http://127.0.0.1:8765/v1`
- Codex bridge: `http://127.0.0.1:8766/v1`

Provider APIs often use OAuth2 and provider-specific message formats, which most tools don't speak natively. This gateway handles:

- **OAuth token refresh** automatically (no manual token copy-paste)
- **Protocol translation** — exposes an OpenAI-compatible `/v1/chat/completions` endpoint on `localhost`
- **Concurrency control** — caps parallel upstream requests and coalesces identical calls
- **True streaming** — forwards Kimi and Codex output incrementally
- **Transparent failover** — retries on 502/429/503 with exponential backoff

## Why?

Hermes, Codex, WorkBuddy and other coding assistants can use one local OpenAI-compatible endpoint while the gateway handles provider authentication and protocol translation.

```
Hermes / OpenAI client
       │  OpenAI protocol
       ▼
┌──────────────────────┐
│  Code Agent Gateway  │  ← this project
│  http://127.0.0.1:8765│
└──────────────────────┘
       │  Anthropic Messages API + OAuth
       ▼
   https://api.kimi.com/coding
```

## Quick Start

### 1. Clone & Configure

```bash
git clone https://github.com/ai-code-master/code-agent-gateway.git
cd code-agent-gateway
cp .env.example .env
# Edit .env and set KCP_CLIENT_ID
```

### 2. Get OAuth Credentials

You need a valid Kimi Code OAuth token. The proxy reads it from `~/.kimi-code/credentials/kimi-code.json` (same format as the official Kimi CLI).

If you already use [Kimi CLI](https://kimi.com), the credentials file usually exists.

### 3. Run

```bash
./start.sh
```

Or directly:

```bash
python3 kimi_code_proxy.py
```

The proxy listens on `http://127.0.0.1:8765` by default.

The gateway is intentionally localhost-only. Do not change `KCP_HOST` to
`0.0.0.0` unless you add authentication, TLS, rate limiting, and a trusted
network boundary yourself.

### 4. Configure Your Client

Point your client to the proxy:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8765
export OPENAI_API_KEY=kimi-code-oauth   # any non-empty string works
```

For **Hermes**, add to `~/.hermes/.env`:

```bash
KIMI_BASE_URL=http://127.0.0.1:8765
KIMI_API_KEY=kimi-code-oauth
```

## Configuration

All settings are via environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| `KCP_CLIENT_ID` | *(required)* | OAuth client ID |
| `KCP_CREDENTIALS_PATH` | `~/.kimi-code/credentials/kimi-code.json` | Path to Kimi OAuth credentials |
| `KCP_DEVICE_ID_PATH` | `~/.kimi-code/device_id` | Path to device ID file |
| `KCP_AUTH_ENDPOINT` | `https://auth.kimi.com/api/oauth/token` | OAuth token endpoint |
| `KCP_UPSTREAM_BASE` | `https://api.kimi.com/coding` | Kimi Code API base URL |
| `KCP_HOST` | `127.0.0.1` | Proxy listen host |
| `KCP_PORT` | `8765` | Proxy listen port |
| `KCP_MAX_CONCURRENT` | `30` | Max concurrent upstream requests |
| `KCP_LOG_DIR` | `~/.hermes/logs` | Log directory |
| `KCP_DEVICE_NAME` | `CodeAgentGateway` | Override to hide real device name |

### Model and executable discovery

The Kimi endpoint keeps the provider's `/v1/models` list. The Codex bridge
discovers models from the locally installed Codex App Server and exposes them
as `codex/<model-id>` in addition to the stable aliases above. Discovery is
best-effort and falls back to the stable aliases when the Codex CLI is not
available.

To override the Codex executable when it is not on `PATH`, set:

```bash
export CODEX_BIN=/absolute/path/to/codex
```

## Run as macOS Service (launchd)

Copy the provided plist template and update paths:

```bash
./launchd/install.sh
# Edit the plist to set the correct WorkingDirectory and ProgramArguments
launchctl print gui/$(id -u)/io.github.code-agent-gateway
```

Install the Codex bridge separately:

```bash
./launchd/install-codex.sh
```

The Codex bridge uses the current `codex login` session and exposes stable
aliases plus any models discovered from the installed Codex CLI:

- `codex-spark` → `gpt-5.3-codex-spark` with High reasoning by default
- `codex-sol` → `gpt-5.6-sol` with High reasoning by default
- `codex-terra` → `gpt-5.6-terra` with Medium reasoning by default
- `codex-luna` → `gpt-5.6-luna` with Low reasoning by default

It translates WorkBuddy Chat Completions requests and tool definitions to the Codex App Server protocol. The bridge listens on localhost only and does not require an OpenAI API key.

## Health Check

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8766/healthz
```

## License

MIT
