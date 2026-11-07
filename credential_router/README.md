# credential-router

本地凭证代理。把上游 LLM API key 集中加密存储在本地，按 `proxy_key` 路由
请求并注入对应真实凭证。纯本地二进制，无外部依赖服务。

## 概述

credential-router 是一个单进程网关，监听两个端口：

| 端口 | 用途 |
|---|---|
| `:8080` (proxy) | 客户端调用入口，按 `proxy_key` 鉴权后注入真实 API key 转发上游 |
| `:8081` (admin) | 增删改查凭据、查看状态、轮转密钥的管理 API |

绑定地址默认 `127.0.0.1`，仅本机访问。配置里改成 `0.0.0.0` 之前请先读完
[`CONFIG.md`](CONFIG.md) 关于 SSRF / host 白名单的章节。

## 环境要求

- Go ≥ 1.23.3（`go version`）
- C 工具链（`gcc` 或 `clang`）。`github.com/mattn/go-sqlite3` 是 CGO 依赖，
  没有 C 编译器会编译失败
- 运行环境：Linux / macOS。Windows 上 CGO 链配置有变数，建议在容器里跑

## 快速开始

```bash
# 1. 构建
CGO_ENABLED=1 go build -o bin/credential-router ./cmd/credential-router

# 2. 起配置（最小可跑配置见 config.example.yaml）
cp config.example.yaml config.yaml
$EDITOR config.yaml   # 改 data_dir / backup_dir 之类的必填项

# 3. 跑
./bin/credential-router -config config.yaml
```

启动成功后日志会打印：

```
proxy   listening on 127.0.0.1:8080
admin   listening on 127.0.0.1:8081
```

首次启动前运行 `./scripts/start.sh install`，该脚本只负责 `go build` 和（可选）写
systemd unit。密钥生成全部由二进制在首次启动时完成（`keystore.SelfInit`）：
从 config 读 `crypto_mode`（默认 `aes`），生成随机 S1 并写入 `data_dir/secrets/s1.bin.1`，
写入 `crypto_mode` 文件，然后生成随机 S2 和 DEK，用 S1+S2+S3 派生 KEK 并 wrap DEK。
S2 和 WrappedDEK 存入 SQLite `key_metadata` 表。明文 DEK 不落盘。无需手动迁移。

## 部署

**生产部署目标 = Linux + systemd**。`./scripts/start.sh install --systemd` 写 `/etc/systemd/system/credential-router.service` + 跑 `daemon-reload`，是 Linux-only 行为（systemd 是 Linux 项目）。

非 systemd host（macOS / BSD / Linux-without-systemd / Docker）走 pidfile + 前台 fallback。Windows / k8s 不支持（bash 脚本 + POSIX 路径假设）。完整部署矩阵见 `AGENTS.md` 的 `## 部署 / Deployment` section。

## 配置

精简可跑配置见 [`config.example.yaml`](config.example.yaml)。所有字段的语义
和默认值在 [`CONFIG.md`](CONFIG.md)，包含 SSRF 策略、缓存、轮转、
admin 校验等高级调优项。配置热加载：不支持——改完重启进程。

## 调用约定

### URL 格式

```
http://127.0.0.1:8080/proxy/{base64url(api_base)}/{rest}
```

`{api_base}` 必须是创建凭据时登记的 `api_base` 的 base64url 编码（RawURLEncoding，
不带 `=` padding）。`{rest}` 透传给上游——例如对 OpenAI 等价于
`https://api.openai.com/v1/chat/completions`，就拼成
`/proxy/{base64url("https://api.openai.com/v1")}/chat/completions`。

### `proxy_key` 来源（按优先级）

1. `Authorization: Bearer <proxy_key>`
2. `X-Api-Key: <proxy_key>`
3. `X-Goog-Api-Key: <proxy_key>`（仅当 `auth_type=google`）

三个来源都缺 `proxy_key` → 401 `proxy_key required`。`proxy_key` 查不到
对应凭据 → 401 `unknown proxy_key`。请求体内的客户端占位 key（如果有）
写成 `***` 即可，代理会按 `auth_type` 自动注入真实 key：

| `auth_type` | 注入位置 |
|---|---|
| `openai` | `Authorization: Bearer {key}` |
| `anthropic` | `x-api-key: {key}` |
| `google` | `x-goog-api-key: {key}` |

注入前会从请求头里剥掉 `Authorization` / `X-Api-Key` / `X-Goog-Api-Key` 三个
header 族，避免把客户端凭据透传给上游。

### `proxy_key` 格式

`cr_pk_` + 43 字符 base64url（32 字节随机数，`RawURLEncoding`，无 `=` padding）。
由服务端在 `POST /v1/credentials` 时生成，客户端不可推导。找回方式只有
重读 admin GET。

### 错误码

proxy 和 admin 端口都使用统一的 JSON envelope：

- 成功：`{"status":"ok","data":<body>}`
- 失败：`{"status":"error","error":{"code":"<machine_code>","message":"<text>","op":"<op>"}}`

admin 在 503 响应上自动加 `Retry-After: 1` 头。

| 状态码 | 含义 |
|---|---|
| 400 | base64 解码失败 / `api_base` 不是合法 URL / admin 字段缺失或非法 |
| 401 | `proxy_key` 缺失或查不到对应凭据 |
| 404 | 路径错（不是 `/proxy/...` 形式）或凭据已删除 |
| 409 | admin 创建时 `(user_id, api_base, key_tag)` 三元组冲突 / 并发写冲突 / 轮轮进行中 |
| 413 | 请求体超过 `server.max_request_bytes` |
| 500 | 内部错误（如凭据注入失败） |
| 502 | 上游连接失败 / 上游读失败 / 上游响应超过 `server.max_response_bytes`（背压触发）/ 客户端断连取消上游 |
| 503 | 凭据查询内部错误（凭据服务不可用） |
| 504 | 上游超时（`upstream_timeout_ms`） |

### 流式

SSE / chunked 透传。`http.MaxBytesReader` 在客户端 ↔ 代理 ↔ 上游两侧都做
背压；客户端断连通过 `outReq.Context` 取消上游，释放连接。

### proxy 行为细节

- **不跟随上游 3xx**：`http.Client.CheckRedirect` 返回 `http.ErrUseLastResponse`，
  上游 3xx 响应原样透传给客户端，proxy 不会做二次跳转。
- **Hop-by-hop 头剥离**：转发前移除 `Connection` / `Keep-Alive` / `Proxy-Authenticate`
  / `Proxy-Authorization` / `Te` / `Trailer` / `Transfer-Encoding` / `Upgrade`
  + `Connection` 头列出的任何附加头；同时移除客户端的 `Host` 头并替换为
  上游 URL 的 host。
- **凭据头剥离**：转发前无条件移除 `Authorization` / `X-Api-Key` /
  `X-Goog-Api-Key` 三个 header 族，再按 `auth_type` 注入真实 key，避免把
  客户端凭据透传给上游。

### 健康检查端

- proxy 端口：`GET /health` → `200 ok`（无 body 解析，纯存活探针）
- admin 端口：`GET /v1/health` → 详细健康信息 JSON：
  - `status`（`ok` / `degraded` / `unavailable`）
  - `keystore` / `manager_ready`
  - `convergence_state`（`idle` / `running` / `completed` / `failed`）
  - `convergence_error` / `convergence_started_at` / `convergence_finished_at`
  - `build_info`

## 管理 API（admin，:8081）

admin 端点契约的完整列表见 `internal/credmgr/admin/` 下的源码注释和
`AGENTS.md`。最常用端点：

- `POST /v1/credentials` — 创建凭据，返回 `proxy_key`
- `GET /v1/credentials?limit=&offset=` — 列表（不含 `api_key`）
- `GET /v1/credentials/{proxy_key}` — 单条详情（含明文 `api_key`，仅此端点回显）
- `PUT /v1/credentials/{proxy_key}` — **部分更新**：只改 `api_key` 和 `auth_type`，`user_id` / `api_base` / `key_tag` 是不可变身份字段（请求 body 里带了也会被忽略，要换身份请 DELETE 后重建）。last-write-wins，不需要 `If-Match`。
- `DELETE /v1/credentials/{proxy_key}` — 删除（last-write-wins）
- `POST /v1/keystore/shards {"action":"rotate-s1"}` — 轮转 S1 shard
- `POST /v1/keystore/shards {"action":"rotate-s2"}` — 轮转 S2 shard（可选 body 带 `s2` 字段手动指定，缺省随机生成）
- `POST /v1/keystore/rotate-dek` — 轮转 DEK
- `GET /v1/keystore/status` — 轮转进度（`rotation_state` ∈ `{idle, swap_pending, reencrypting, ready_to_commit}` + `straggler_count`）

> **⚠️ admin API 无认证**：admin 端点默认不鉴权，仅靠 bind 地址（`127.0.0.1`）限制
> 访问。改成 `0.0.0.0` 或暴露到网络前，必须前置 reverse proxy 加 basic auth / mTLS /
> IP 白名单。直接暴露 admin 端口等于把所有凭据的增删改查权交给同网任何人。

## 安全模型摘要

- **存储**：API key 落盘前用 DEK 加密（AES-128-GCM）；DEK 用 KEK 加密
  后分到两份文件 + 一份三方备份
- **密钥分片**：KEK 三分片（`S1` 本地文件 + `S2` 存于 SQLite key_metadata + `S3` 硬编码
  emergency shard）共同解密 DEK。S2 在首次启动时由 `crypto/rand` 随机生成并写入
  SQLite `key_metadata` 表，与 DB 一起落盘。S3 是可见性取舍：源代码里读得到，但只在
  `S1`+`S2` 都丢时才需要
- **轮转**：DEK 周期轮转（`rotation.period`，默认 720h）；轮转期间
  Phase A 重加密 + 排空 + Phase B 切换。轮转进度通过 `/v1/keystore/status`
  暴露
- **SSRF**：默认关闭 dialer SSRF 防护（产品以 appliance 部署，模型推理出口
  在可信网内）。暴露到不可信网络前必须开 `ssrf.dial_check` 并配置
  `ssrf.allowed_hosts`
- **进程退出**：关闭时按 `server.shutdown_zero_budget`（默认 5s）尽最大努力
  清零内存里的明文 key

## 数据目录布局

启动时若 `data_dir` 不存在会自动创建。典型布局（`data_dir: ./data`）：

```
data/
├── credentials.db           # SQLite，存元数据 + 加密后的 api_key
├── credentials.db-wal       # SQLite WAL
├── credentials.db-shm       # SQLite WAL shared memory（WAL 模式下自动生成）
├── .lock                    # 单实例 flock 锁文件（进程生命周期持有）
├── router.pid               # PID 文件，格式 `<pid>:<unix_ts>`（重启时自动清理）
└── secrets/
    ├── s1.bin.1             # S1 分片（KEK 分片 1，本地；.1 = 初始版本，轮转后递增）
    ├── dek.bin              # DEK（16B，运行时由 KEK 解包）
    └── crypto_mode          # 1 byte 二进制：0x01=AES / 0x02=SM4

backups/                     # backup_dir（配置项，与 data_dir 独立）
└── ...
```

数据库 schema 启动时幂等 bootstrap（`CREATE TABLE IF NOT EXISTS`），不维护
版本化迁移 CLI。schema 变更以"启动即生效"为约束，新装直接获得新 schema，
老装升级后下次启动自动补齐新增表 / 字段。

## 构建与发布

```bash
# 本地 dev build（产物落到 dist/bin/credential-router）
CGO_ENABLED=1 go build -o dist/bin/credential-router ./cmd/credential-router

# 交叉编译（产物落到 dist/bin/credential-router_{GOOS}_{GOARCH}，VERSION 取 git tag）
VERSION=v1.2.3 ./scripts/build.sh x86_64   # 或 arm64
```

支持 arch：`x86_64`/`amd64` 和 `arm64`/`aarch64`。脚本会拒绝 go < 1.23.3。
开发/测试/调试相关 build tag 与 profiling build 见 `AGENTS.md`。

## 依赖

直接依赖（均为生产构建带入）：

| 包 | 协议 | 用途 |
|---|---|---|
| `github.com/mattn/go-sqlite3` | MIT | SQLite 驱动（CGO） |
| `github.com/tjfoc/gmsm` | Apache-2.0 | SM4-GCM 国密支持 |
| `gopkg.in/yaml.v3` | MIT | 配置文件解析 |
| `golang.org/x/crypto` | BSD-3 | 通用密码学原语 |

完整依赖树见 `go.sum`。所有依赖均为宽松协议（MIT / BSD-3 / Apache-2.0），
商用分发无需单独授权。