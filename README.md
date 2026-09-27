# Code Agent Gateway

把本机已经登录的 AI 编程订阅统一转换为 OpenAI 兼容接口。

Use locally signed-in AI coding subscriptions through one OpenAI-compatible API.

[中文](#中文) | [English](#english)

> [!IMPORTANT]
> 本项目默认只监听 `127.0.0.1`。它会复用本机登录凭据，请不要暴露到局域网或互联网。

---

## 中文

### 这是什么

Code Agent Gateway 是一个本地网关。它复用已登录的 Kimi Code 和 Codex CLI
会话，对外提供统一的 OpenAI 兼容 API。Hermes、WorkBuddy 或其他 OpenAI
兼容客户端只需连接一个本地地址。

```text
Hermes / WorkBuddy / OpenAI-compatible client
                      |
                      v
          http://127.0.0.1:8765/v1
                      |
              Code Agent Gateway
                 /           \
                v             v
          Kimi Code        Codex CLI
          OAuth session    login session
```

### 能实现什么

- Kimi Code 和 Codex 共用 `http://127.0.0.1:8765/v1`。
- 根据 `model` 自动选择提供商，不再需要两个端口。
- 自动寻找 Kimi Code 凭据、Codex 可执行文件和可用模型。
- 支持 Chat Completions、Codex Responses API、流式输出和函数工具。
- 复用本机已登录的会话，无需手工复制短期 access token。
- Kimi token 提前刷新，凭据文件原子写入，避免并发覆盖和文件损坏。
- 有界 HTTP 工作池、统一并发门控、有界队列和过载 `503`。
- Kimi HTTPS 连接池、指数退避、随机抖动、`Retry-After` 和熔断保护。
- Codex App Server 进程池，避免每次请求重新启动 CLI。
- 提供健康、模型缓存、队列、连接复用、熔断和提供商指标。
- 优雅停机，先拒绝新请求，再等待正在运行的请求。

### 支持的 API

| 接口 | Kimi Code | Codex |
|---|---:|---:|
| `GET /v1/models` | 支持 | 支持 |
| `POST /v1/chat/completions` | 支持 | 支持 |
| `POST /v1/responses` | 不支持 | 支持 |
| Streaming | 支持 | 支持 |
| Function tools | 原样转发 | 转换为客户端工具调用 |

Codex 提供 `codex-luna`、`codex-sol` 等稳定别名，以及 `codex/<model-id>`
形式的动态模型名。实际列表以 `/v1/models` 为准。

### 环境要求

- Python 3.9 或更高版本，无第三方 Python 运行依赖。
- 至少安装并登录 Kimi Code 或 Codex CLI 中的一个。
- 项目主要在 macOS 上开发和验证；`launchd` 安装方式仅适用于 macOS。

> [!NOTE]
> 网关不会创建或附带订阅。请先在官方 CLI 中完成登录，并确认 CLI 本身可以正常使用。

### 快速开始

```bash
git clone https://github.com/ai-code-master/code-agent-gateway.git
cd code-agent-gateway
cp .env.example .env
chmod 600 .env
./start.sh
```

另开终端检查：

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/v1/models
```

默认情况下：

- Kimi 凭据从 `~/.kimi-code/credentials/kimi-code.json` 读取。
- Codex 从 `PATH` 和常见位置中查找，并使用当前 `codex login` 会话。
- 自动查找失败时，设置 `CODEX_BIN=/absolute/path/to/codex`。
- `CAG_CLIENT_ID` 只用于 Kimi OAuth 自动刷新。未设置时，现有 token 可使用到过期，之后需重新登录。

### 连接 Hermes、WorkBuddy 或其他客户端

在客户端中选择“OpenAI Compatible”：

| 配置项 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8765/v1` |
| API Key | `local-gateway` |
| Model | 从 `/v1/models` 中选择 |

API Key 可以是任意非空字符串。网关只监听 localhost，不校验该值；部分客户端
不允许留空，因此推荐 `local-gateway`。

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8765/v1
export OPENAI_API_KEY=local-gateway
```

Hermes 如果使用 Kimi 风格变量：

```bash
export KIMI_BASE_URL=http://127.0.0.1:8765
export KIMI_API_KEY=local-gateway
```

### 调用示例

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-gateway' \
  -d '{
    "model": "kimi-for-coding",
    "messages": [{"role": "user", "content": "请解释这段代码"}],
    "stream": false
  }'
```

Codex Responses API：

```bash
curl http://127.0.0.1:8765/v1/responses \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-gateway' \
  -d '{
    "model": "codex-luna",
    "input": "Return exactly OK",
    "stream": false
  }'
```

### macOS 后台服务

```bash
./launchd/install.sh
launchctl print gui/$(id -u)/io.github.code-agent-gateway
```

服务会在登录后自动启动，异常退出后重新拉起。只会安装统一的 `8765` 端口服务。

### 常用配置

配置从 `.env` 或进程环境变量中读取。完整列表见 [`.env.example`](.env.example)。

| 变量 | 默认值 | 用途 |
|---|---:|---|
| `CAG_HOST` | `127.0.0.1` | 监听地址 |
| `CAG_PORT` | `8765` | 统一端口 |
| `CAG_MAX_CONCURRENT` | `30` | 全局模型请求并发上限 |
| `CAG_HTTP_WORKERS` | `64` | HTTP 工作线程数 |
| `CAG_HTTP_PENDING` | `128` | HTTP 待处理队列上限 |
| `CAG_CODEX_POOL_SIZE` | `2` | 可复用 Codex 进程数 |
| `CAG_CODEX_QUEUE_TIMEOUT` | `30` | Codex 进程池等待时间 |
| `CAG_CODEX_PROXY` | 空 | 仅供 Codex 子进程使用的代理，例如 `http://127.0.0.1:7897` |
| `CAG_UPSTREAM_TIMEOUT` | `600` | 单次上游请超时 |
| `CAG_MAX_RETRIES` | `2` | 可重试错误的重试次数 |
| `CAG_CIRCUIT_FAILURE_THRESHOLD` | `5` | 熔断前的连续失败阈值 |
| `CAG_ENABLE_CACHE` | `1` | 是否开启保守响应缓存 |
| `CAG_LOG_DIR` | `~/.code-agent-gateway/logs` | 日志目录 |

`SIGHUP` 或 `/admin/reload` 可重载大部分限制、缓存和阈值。端口、监听地址、
凭据路径和 HTTP 工作池大小变更后需重启。

### 运行状态和性能

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/metrics
```

`/healthz` 分别报告 Kimi 和 Codex，同时显示 Codex 进程池、Kimi 连接池、
模型缓存、熔断状态和活跃请求。

v3.2 本地 loopback 实测：

| 场景 | 优化前 | v3.2 |
|---|---:|---:|
| `/healthz`，1000 请求 / 100 并发 | 948 成功，52 超时 | 1000 成功，0 超时 |
| `/healthz` 吞吐 | 约 948 RPS | 约 2546 RPS |
| `/v1/models`，300 请求 / 30 并发 | p95 约 3.79 s | p95 约 16.7 ms |
| Kimi HTTPS 连接 | 每次新建 | 已验证连接复用 |

> [!NOTE]
> 以上是网关本身的本地实测，不是性能保证。生成速度仍取决于上游服务、模型、订阅和网络。

### 缓存行为

响应缓存只接受：非流式、无 tools/tool choice、`temperature: 0`、完整请求内容
相同的请求。保守策略优先保证正确性，避免复用非确定性结果。

### 安全和隐私

- 默认只绑定 `127.0.0.1`，不提供入站用户认证。
- `.env`、JSON 凭据、日志和 Python 缓存均被 Git 忽略。
- 凭据仅从用户本机读取，不会被复制到项目仓库。
- 日志默认不记录请求和响应正文；开启 `CAG_DEBUG_BODY` 前请评估隐私风险。
- Codex 使用 `read-only` sandbox 和 `approvalPolicy: never`；网关返回工具调用，不代替客户端执行。
- 工作目录必须是已存在的本机绝对路径。请只连接你信任的本地客户端。
- 请遵守 Kimi、OpenAI 以及所使用订阅的服务条款。

> [!WARNING]
> 不要把 `CAG_HOST` 改为 `0.0.0.0`，也不要通过反向代理、内网穿透或端口映射暴露本服务。

### 常见问题

**API Key 可以留空吗？**
网关不校验该值，但很多客户端不允许留空，推荐填 `local-gateway`。

**为什么刚启动时模型列表只有 Codex 别名？**
模型发现在后台异步完成。数秒后重新请求 `/v1/models`。

**Codex 为什么慢？**
Codex 是上游长任务。网关会保持其他请求可用，但不能缩短模型的思考时间。

**出现 `503` 或长时间 pending 怎么办？**
查看 `/healthz` 中的 `concurrent_active`、`codex_pool` 和熔断状态，再按机器资源调整
`CAG_MAX_CONCURRENT`、`CAG_CODEX_POOL_SIZE` 和队列超时。

**这是云端 API 网关吗？**
不是。它是单机、单用户、localhost-only 的编程订阅适配器，不提供账号、计费、租户和远程访问管理。

---

## English

### What it is

Code Agent Gateway is a local adapter that reuses signed-in Kimi Code and Codex CLI
sessions and exposes them through one OpenAI-compatible API. Hermes, WorkBuddy, and
other compatible clients only need one local endpoint.

### Features

- One endpoint for Kimi Code and Codex: `http://127.0.0.1:8765/v1`.
- Automatic provider routing based on the requested model.
- Automatic discovery of Kimi credentials, the Codex executable, and available models.
- Chat Completions, Codex Responses API, streaming, and function tools.
- Kimi token refresh with atomic credential-file updates.
- Reusable Codex App Server processes and pooled Kimi HTTPS connections.
- Bounded HTTP workers, shared admission control, queues, and explicit overload responses.
- Retry backoff with jitter, `Retry-After`, circuit breaking, and graceful shutdown.
- Health, metrics, provider latency, pool usage, queue wait, and circuit status.

### Supported APIs

| Endpoint | Kimi Code | Codex |
|---|---:|---:|
| `GET /v1/models` | Yes | Yes |
| `POST /v1/chat/completions` | Yes | Yes |
| `POST /v1/responses` | No | Yes |
| Streaming | Yes | Yes |
| Function tools | Pass-through | Converted to client tool calls |

Codex has stable aliases such as `codex-luna` and `codex-sol`, plus dynamically
discovered names in the `codex/<model-id>` format. Treat `/v1/models` as the source of truth.

### Requirements and quick start

- Python 3.9 or newer; no third-party Python runtime dependencies.
- At least one locally installed and signed-in provider: Kimi Code or Codex CLI.
- Primarily developed and tested on macOS. The `launchd` installer is macOS-only.

> [!NOTE]
> The gateway does not create or include a subscription. Sign in with the official CLI first and verify that the CLI works on its own.

```bash
git clone https://github.com/ai-code-master/code-agent-gateway.git
cd code-agent-gateway
cp .env.example .env
chmod 600 .env
./start.sh
```

Verify from another terminal:

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/v1/models
```

Defaults and overrides:

- Kimi credentials: `~/.kimi-code/credentials/kimi-code.json`.
- Codex: resolved from `PATH` and common locations, using the current `codex login` session.
- Set `CODEX_BIN=/absolute/path/to/codex` if executable discovery fails.
- `CAG_CLIENT_ID` is only needed for automatic Kimi OAuth refresh. Without it, the current token works until it expires and a new login is required.

### Connect Hermes, WorkBuddy, or another client

Select an OpenAI-compatible provider:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8765/v1` |
| API Key | `local-gateway` |
| Model | Any name returned by `/v1/models` |

The gateway ignores the inbound API key because it only listens on localhost. Many clients
still require a non-empty value, so `local-gateway` is recommended.

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8765/v1
export OPENAI_API_KEY=local-gateway
```

For Hermes installations using Kimi-style variables:

```bash
export KIMI_BASE_URL=http://127.0.0.1:8765
export KIMI_API_KEY=local-gateway
```

### API examples

```bash
curl http://127.0.0.1:8765/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-gateway' \
  -d '{
    "model": "kimi-for-coding",
    "messages": [{"role": "user", "content": "Explain this code"}],
    "stream": false
  }'
```

Codex Responses API:

```bash
curl http://127.0.0.1:8765/v1/responses \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer local-gateway' \
  -d '{
    "model": "codex-luna",
    "input": "Return exactly OK",
    "stream": false
  }'
```

### Run as a macOS service

```bash
./launchd/install.sh
launchctl print gui/$(id -u)/io.github.code-agent-gateway
```

The service starts on login and restarts after an unexpected exit. Only the unified `8765`
service is installed.

### Common configuration

Configuration is loaded from `.env` or process environment variables. See
[`.env.example`](.env.example) for the complete list.

| Variable | Default | Purpose |
|---|---:|---|
| `CAG_HOST` | `127.0.0.1` | Listen address |
| `CAG_PORT` | `8765` | Unified port |
| `CAG_MAX_CONCURRENT` | `30` | Global model-request concurrency |
| `CAG_HTTP_WORKERS` | `64` | Bounded HTTP workers |
| `CAG_HTTP_PENDING` | `128` | Pending HTTP queue size |
| `CAG_CODEX_POOL_SIZE` | `2` | Reusable Codex processes |
| `CAG_CODEX_QUEUE_TIMEOUT` | `30` | Codex pool wait timeout |
| `CAG_CODEX_PROXY` | empty | Proxy used only by Codex children, for example `http://127.0.0.1:7897` |
| `CAG_UPSTREAM_TIMEOUT` | `600` | Per-request upstream timeout |
| `CAG_MAX_RETRIES` | `2` | Retry count for retryable failures |
| `CAG_CIRCUIT_FAILURE_THRESHOLD` | `5` | Failures before circuit opening |
| `CAG_ENABLE_CACHE` | `1` | Conservative response cache |
| `CAG_LOG_DIR` | `~/.code-agent-gateway/logs` | Local log directory |

Use `SIGHUP` or `/admin/reload` to reload most limits, cache settings, and thresholds.
Listen address, port, credential paths, and HTTP worker sizing require a restart.

### Health and measured performance

```bash
curl http://127.0.0.1:8765/healthz
curl http://127.0.0.1:8765/metrics
```

`/healthz` reports Kimi and Codex independently, including Codex process-pool state,
Kimi connection-pool state, model cache, circuit state, and active requests.

Local loopback measurements for v3.2:

| Scenario | Before | v3.2 |
|---|---:|---:|
| `/healthz`, 1,000 requests at concurrency 100 | 948 success, 52 timeouts | 1,000 success, 0 timeouts |
| `/healthz` throughput | about 948 RPS | about 2,546 RPS |
| `/v1/models`, 300 requests at concurrency 30 | p95 about 3.79 s | p95 about 16.7 ms |
| Kimi HTTPS connections | New connection per request | Verified connection reuse |

> [!NOTE]
> These are local gateway measurements, not guarantees. Generation latency still depends on the upstream service, model, subscription, and network.

### Cache behavior

The response cache only accepts requests that are non-streaming, contain no tools or tool
choice, explicitly set `temperature` to `0`, and have an identical complete request payload.
This prevents nondeterministic responses from being reused incorrectly.

### Security and privacy

- The server binds to `127.0.0.1` by default and implements no inbound authentication.
- `.env`, JSON credentials, logs, cache files, and Python caches are ignored by Git.
- Credentials remain on the local machine and are never copied into the repository.
- Request and response bodies are not logged by default. Review privacy implications before enabling `CAG_DEBUG_BODY`.
- Codex App Server uses a `read-only` sandbox and `approvalPolicy: never`; the gateway returns client tool calls instead of executing them.
- Working directories must be existing absolute local paths. Only connect trusted local clients.
- Use the project in accordance with the terms of Kimi, OpenAI, and your subscription.

> [!WARNING]
> Do not change `CAG_HOST` to `0.0.0.0` or expose this service through a reverse proxy, tunnel, or port forward.

### Troubleshooting

**Can the API key be empty?**
The gateway ignores it, but many clients require a value. Use `local-gateway`.

**Why do I initially see only Codex aliases?**
Model discovery runs asynchronously. Request `/v1/models` again after a few seconds.

**Why can a Codex request take a long time?**
Codex is an upstream long-running task. The gateway keeps unrelated requests responsive but
cannot reduce the model's own reasoning time.

**What should I check after a `503` or a long pending request?**
Inspect `concurrent_active`, `codex_pool`, and circuit state in `/healthz`, then tune
`CAG_MAX_CONCURRENT`, `CAG_CODEX_POOL_SIZE`, and queue timeouts for the machine.

**Is this a cloud API gateway?**
No. It is a single-machine, single-user, localhost-only coding-subscription adapter. It does
not provide accounts, billing, tenancy, or remote access management.
