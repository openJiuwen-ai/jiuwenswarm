# 项目偏好 / Project Preferences

## 测试 / Testing
- 用 CGO + test tag: `CGO_ENABLED=1 go test -tags test ./...`
  - `-tags test` 必须带——外部 white-box 测试套件依赖 `internal/*/testing.go` 中的 ForTesting hook；这些文件用 `//go:build test` 门控，从生产构建中排除。忘加 `-tags test` 会让 `go test` 在 vet 阶段报 undefined。

### TPCC 两种跑法 / TPCC run modes
`tests/setup_test.go::TestMain` 一次性 build 两份 binary（生产 + instrumented），后续根据 tag 决定跑哪种。

#### 1. 纯跑（不开 profiling）/ plain workload — no pprof scraping
- 跑 `tests/concurrency_tpcc_test.go`，spawn 的是 instrumented binary（pprof 端点起得来但测试不 scrape）
- 命令：
  ```
  TPCC_DURATION=3s TPCC_SEED=50 \
    CGO_ENABLED=1 go test -tags test -timeout=60s \
      -run TestTPCCConcurrency ./tests/...
  ```

#### 2. 开 profiling（scrape /debug/pprof/*）/ instrumented workload with pprof assertions
- 跑 `tests/concurrency_tpcc_instrumented_test.go`（`//go:build cgo && instrumented`），需要同时 `cgo` + `instrumented` + `test` 三个 tag
- 命令：
  ```
  TPCC_DURATION=3s TPCC_SEED=50 \
    CGO_ENABLED=1 go test -tags 'cgo instrumented test' -timeout=60s \
      -run TestTPCCInstrumentedAssertions ./tests/...
  ```

### 调试相关 build tags / Build tags for debugging
- `-tags test`：启用 `internal/*/testing.go` 中的 ForTesting hook（白盒测试用，生产绝不带）
- `-tags instrumented`：启用 `internal/platform/instrument/pprof` 包 + `cmd/credential-router/profiling.go`，spawn 的 binary 暴露 `/debug/pprof/*`（TPCC 性能 profiling 用，生产绝不带）
- 两者正交，可同时带：`go test -tags 'test instrumented' ...`

### 测试 binary 注入 / Test binary injection
`tests/setup_test.go::TestMain` 默认 spawn 两份：instrumented（`credential-router-test`，带 pprof 端点）和 production（`credential-router-prod-test`，无 pprof）。通过环境变量可跳过 build，直接复用 CI 缓存的产物：

- `CREDENTIAL_ROUTER_BIN=<path>`：覆盖 instrumented binary（`credential-router-test`），路径相对于模块根或绝对路径均可
- `CREDENTIAL_ROUTER_PROD_BIN=<path>`：覆盖 production binary（`credential-router-prod-test`）

设置后 `TestMain` 不再 `go build`，启动时间大幅缩短。CI cache 里 binary 但源码有改动时记得同步清缓存。

## 脚本 / Scripts

`scripts/` 下 4 个 wrapper。**只有 `start.sh --systemd` 走全局 systemd 状态**；其他都是 LOCAL（只动 `data/` `dist/` `/tmp/fuzz.*.log`）。

### `scripts/build.sh` —— 跨平台 cross-compile + package

- 完全 LOCAL：`rm -rf $DIST && mkdir $DIST` → `go build` 到 `dist/bin/credential-router_<os>_<arch>` → `tar -czf` 到 `dist/*.tar.gz`
- `CGO_ENABLED=1`（`internal/credmgr` 全部文件有 `//go:build cgo` 约束，依赖 `mattn/go-sqlite3`；交叉编译 Windows 需设 `CC=x86_64-w64-mingw32-gcc`）
- 用法：`VERSION=v1.2.3 ./scripts/build.sh x86_64`（或 `arm64`）

### `scripts/run-tests.sh` —— go test + 子进程 cleanup

- 完全 LOCAL：`go test -tags test -count=1 -timeout=180s "$@" ./tests/...`
- `trap cleanup EXIT INT TERM QUIT HUP` 在退出时 `pkill -9 -f "credential-router-test"` + `pkill -9 -f "dist/bin/credential-router_"` + `kill -- -$pid`（杀 process group）
- **header 显式声明 known hole**：SIGKILL wrapper 自身会漏子进程（go test 通过 bash / `exec.Cmd` 重新 parent 进程，绕过 `Pdeathsig`），需要操作员手动 `pkill -9 -f credential-router-test`
- `pkill -f "dist/bin/credential-router_"` 带尾部下划线，只匹配带 arch 后缀的测试 binary（`credential-router_linux_amd64`），不会误杀名为 `credential-router` 的生产 binary

### `scripts/fuzz.sh` —— `go test -fuzz` per target

- 完全 LOCAL：每个 package 内的每个 `Fuzz*` target 跑 `<preset>` 时长
- 写 log 到 `/tmp/fuzz.<pkg>.log`（**残留不清理** —— 失败时全路径 echo 出来供事后看）
- 三个 preset：`quick`（10s/pack）/ `default`（30s/pack）/ `full`（60s/pack）。`FUZZTIME=5s` 可覆盖
- 用法：`./scripts/fuzz.sh quick` 或 `./scripts/fuzz.sh full tests/unit/crypto`

### `scripts/start.sh` ⚠️ 唯一带 GLOBAL 副作用的脚本

`install` action（**永远 = systemd 部署**）：
- 写 `/etc/systemd/system/credential-router.service` + `systemctl daemon-reload` + `chown credential-router:credential-router data/`。非 systemd host 直接报错退出。
- **不编译 binary**：编译由 `./scripts/build.sh` 负责。target host 只需要 bash + systemd + 预编译 binary，不需要 Go toolchain。
- **不创建 `data/` `secrets/`**（除非要 `--force` 删旧文件）；binary 的 `keystore.SelfInit` 首次启动时自己 MkdirAll。`crypto_mode` 从 config 读取（默认 `aes`），首次 init 后磁盘文件为准（不可变）。S2 + DEK 不落盘明文
- **`--force`**：fresh re-init 语义——备份 + 删除 `credentials.db` + `secrets/s1.bin.*` + `secrets/crypto_mode`，binary 下次启动重新 SelfInit。不动 `backup_dir/`
- 安装脚本不写任何密钥文件——S1、crypto_mode、S2、DEK 全部由 binary 首次启动时生成

`start` action：
- `--systemd`：要求 systemd host（PID-1 systemd + `systemctl` 命令），跑 `systemctl start credential-router`
- 不传（默认）：前台 exec `bin -config config`（pidfile + 前台）。**即使在 systemd host 上，默认也走 pidfile + 前台**，避免给没装 unit 的环境引入半生不熟的 systemd 状态。脚本在 `exec` 前 `echo $$ > ${ROOT}/run/credential-router.pid`；`exec` 保留 PID，binary 接管后 PID 不变

`stop` action：
- `--systemd`：要求 systemd host，跑到 `systemctl stop credential-router`
- 不传（默认）：读 `${ROOT}/run/credential-router.pid`（**脚本自管**，start 时 `exec` 前 `echo $$` 写入；exec 保留 PID），SIGTERM（5s 超时）→ SIGKILL。与 binary 在 `${cfg.DataDir}/router.pid` 写的 internal single-instance lock **完全分离**——脚本不碰 binary 的内部锁文件

**Systemd 集成仅 Linux**：systemd 是 Linux 项目；macOS（launchd）/ BSD（rc.d）/ Linux without systemd（Devuan / Artix / Void / Alpine）走 pidfile + 前台 fallback，不影响其他 systemd 配置。Windows 上 `scripts/` 是 bash-only；operator 跑 bash-via-Git-Bash / WSL，或者直接跑 binary（cross-compile 自带 mingw-w64 后 `go build` 出 `.exe`，见部署表）。

用法：
- `install [--force]` — 写 systemd unit + chown（**永远是 systemd 部署**）
- `start [--systemd]` — 前台或 systemctl
- `stop [--systemd]` — pidfile + signal 或 systemctl

data_dir 在 config.yaml 里设，不在脚本 flag。

## 部署 / Deployment

**生产部署目标 = Linux + systemd**。`scripts/start.sh install` 永远写 `/etc/systemd/system/credential-router.service` + `systemctl daemon-reload` + chown data_dir，是 Linux-only 行为（systemd 是 Linux 项目）。非 systemd host 上 `install` 报错。`have_systemd()`（scripts/start.sh）通过 `/run/systemd/system` 目录存在性 + PID-1 双重 gating 检查 systemd host：install 始终要求通过；start/stop 的 `--systemd` flag 也要求通过。

**职责分离**：
- `./scripts/build.sh x86_64` — 编译 binary 到 `dist/bin/credential-router_<os>_<arch>`
- `./scripts/start.sh install` — systemd 部署（target host 不需要 Go toolchain）
- `./scripts/start.sh start [--systemd]` — 起 binary
- `./scripts/start.sh stop [--systemd]` — 停 binary

| 环境 | 支持度 |
|------|--------|
| Linux + systemd（主流发行版） | ✓ 完整支持：`install` 写 systemd unit + chown；`start --systemd` 起；或纯前台 `start` |
| Linux without systemd（Devuan / Artix / Void / Alpine） | `install` 报错（无 systemd）；用前台 `start` + 自写 init script（OpenRC / runit / SysV） |
| macOS | dev only（前台跑 binary，缺 launchd plist） |
| Windows | ✓ binary 工作（cross-compile `GOOS=windows CGO_ENABLED=1 CC=x86_64-w64-mingw32-gcc go build` 出 `.exe`）；脚本需 bash-via-Git-Bash / WSL；systemd N/A |
| Docker / k8s | 仅 binary 本身（不带 init manager / supervisord）；多副本需外部分布式锁替代 singleinstance |

## 端口与运行时探针 / Ports & runtime probes

两个端口都从 `server.bind_address`（默认 `127.0.0.1`）起。wildcard bind (`0.0.0.0` / `::`) 在 `Config.Validate()` 阶段就被拦下来，必须显式配 `server.external_address` 才能跑。

| 端口 | 用途 | 端点 |
|---|---|---|
| `:8080`（或 `proxy.port`） | 代理 / 注入凭据转给上游 | `GET /health` → `200 ok`，纯存活探针，无 body 解析（`internal/proxy/handler.go:78-82`）。其他所有路径都按 `/proxy/{base64url(api_base)}/{rest}` 解析 |
| `:8081`（或 `admin.port`） | 凭据 CRUD + keystore 状态 | `GET /v1/health` → 详细 JSON（`status` / `keystore` / `manager_ready` / `convergence_state` / `convergence_error` / `convergence_started_at` / `convergence_finished_at` / `build_info`）。CRUD + 轮转端点见下"关键 API 约定" |

**admin 无认证**：见"常见踩坑"。这两个端点是 binary 唯一对外暴露的 HTTP 入口，pprof 端点（`-tags instrumented` build）走 admin `:8081/debug/pprof/*`，与 `/v1/health` 同端口不同前缀。

## 真实上游 e2e / Real-key e2e (打真 OpenAI / Anthropic / Google)

`tests/real_creds_test.go` 定义了 3 套"打真实上游"的 e2e 用例，每家 provider 配 3 个环境变量：

| Provider | API key（**必填**） | real_url（可选） | key_tag（可选） |
|---|---|---|---|
| openai | `E2E_OPENAI_API_KEY` | `E2E_OPENAI_REAL_URL` | `E2E_OPENAI_KEY_TAG` |
| anthropic | `E2E_ANTHROPIC_API_KEY` | `E2E_ANTHROPIC_REAL_URL` | `E2E_ANTHROPIC_KEY_TAG` |
| google | `E2E_GOOGLE_API_KEY` | `E2E_GOOGLE_REAL_URL` | `E2E_GOOGLE_KEY_TAG` |

跳过逻辑（`tests/real_creds_test.go:44-47`）：

```go
apiKey := strings.TrimSpace(os.Getenv(spec.EnvKey))
if apiKey == "" {
    t.Skipf("%s not set, skipping real API test", spec.EnvKey)
}
```

- 不设 API key 环境变量 → `t.Skip` 主动跳过；Go test 仍 `PASS`（不计入失败）
- 设了 → 用 `real_url`（或 spec `DefaultURL`）打真上游；`key_tag` 缺省 `"default"`
- `real_url` 缺省值：openai → `https://api.openai.com/v1`，anthropic → `https://api.anthropic.com/v1`，google → `https://generativelanguage.googleapis.com/v1beta`

**默认行为**：本地 dev / 默认 CI 不设 → 整套静默 SKIP，不影响 `go test` 整体通过。
**显式开启**：CI secret 灌 key 后真打，做上线前 / nightly 验证。

### 关键 API 约定 / Key endpoint contracts
- **`POST /v1/credentials`** body（5 个字段，多字段 400 `unknown field`）：
  ```json
  {"user_id":"...","api_base":"...","key_tag":"...","api_key":"...","auth_type":"openai"}
  ```
  - `auth_type` 必须 ∈ `{openai, anthropic, google}`，其他值 400
  - 重复 (user_id, api_base, key_tag) 返回 409 `conflict`
  - 响应 `data` 含 `proxy_key`（服务端 INSERT 时生成，客户端不可推导）+ `proxy_address` + `kek_version`/`dek_version` + `created_at`/`updated_at` + identity 字段（`user_id`/`api_base`/`key_tag`/`auth_type`）；**不含 `id`/`row_version`，不回显 `api_key`**
    - **`proxy_address` 拼接规则**：用 `server.external_address`（如果配置了）或 `server.bind_address`（否则）作为 base，拼上 `/proxy/{b64(api_base)}`。当 `server.bind_address` 是 wildcard (`0.0.0.0` / `::`) 时，`Config.Validate()`（`internal/platform/config.go`）会拒绝启动——wildcard 不是合法的 client 目标地址，binary 不知道外部 endpoint，operator 必须显式配 `server.external_address`（必须是 `http://` 或 `https://` URL）。`proxy_address` 字段有 `json:",omitempty"` 兜底：直接调用 `proxyAddress()` 而绕过 Validate 的代码路径会拿到空字符串而不是 `http://0.0.0.0:8080/proxy/...` 的误导值。启动时若检测到 wildcard bind 仍会记 `WARN` 日志（`cmd/credential-router/main.go:226`）
- **`GET /v1/credentials?limit=&offset=`**：返回 `{count, items, limit, offset}`，**不含 api_key**；每项含 `proxy_key`，**不含 `id`/`row_version`**。`limit` 默认 `50`，最大 `200`（超过静默 clamp 到 `200`，不报错）；`offset` 必须 `≥ 0`，非法整数返回 400
- **`GET /v1/credentials/{proxy_key}`**：单条查询，路径参数是 `proxy_key`（不是 b64key）。未知 `proxy_key` → 404 `not_found`。响应含明文 `api_key`（仅单条 GET 返回），**不含 `id`/`row_version`**
- **`PUT /v1/credentials/{proxy_key}`** 和 **`DELETE /v1/credentials/{proxy_key}`**：**不需要 `If-Match` header**（last-write-wins，外部并发不做乐观锁）。`row_version` 仅保留在服务端内部，用于 Phase A 轮转期间的竞态保护。**PUT 是部分更新**：只改 `api_key` 和 `auth_type`，`user_id` / `api_base` / `key_tag` 是不可变 identity（请求 body 带了这三个字段也会被忽略；想换 identity 请 DELETE 后重建）。PUT body 至少需要 `api_key` 或 `auth_type` 之一非空
- **`proxy_key` 格式**（`internal/credmgr/store/credentials.go` `GenerateProxyKey`）：`"cr_pk_"` + 43 字符 `base64url`（32 字节随机数，`RawURLEncoding`，无 `=` padding）。`proxy_key` 是随机 ID，服务端 INSERT 时生成，与 `user_id` / `api_base` / `key_tag` 无派生关系
- **Proxy URL 格式**（`internal/proxy/parse.go`）：`/proxy/{api_base_b64}/{rest}`，第 1 段永远是 `base64url(api_base)`。proxy_key 三种来源（按优先级）：
  1. `Authorization: Bearer <proxy_key>` header
  2. `X-Api-Key: <proxy_key>` header
  3. `X-Goog-Api-Key: <proxy_key>` header
  - 三个来源都缺 proxy_key → 401 `proxy_key required`；proxy_key 查不到 credential → 401；其余内部错误 → 503
  - **`Authorization: Bearer` 大小写不敏感**（RFC 7235 §2.1）：`bearer xxx` / `BEARER xxx` / `Bearer xxx` 等大小写组合都接受。`X-Api-Key` / `X-Goog-Api-Key` 头保留大小写敏感（与各家 SDK 期望对齐）
  - 注入上游请求前 `stripAuthHeaders(headers)`（`internal/proxy/header_transform.go`）会剥掉 `Authorization`/`X-Api-Key`/`X-Goog-Api-Key` 三个 header 族，避免把客户端凭据透传给上游
- **`POST /v1/keystore/shards {action:"rotate-s1"}`**：body 不需要 `new_s1_version`，服务端 auto-generate 下一个 S1 shard
- **`POST /v1/keystore/shards {action:"rotate-s2"}`**：body 可选带 `s2` 字段手动指定新 S2（缺省由 `crypto/rand` 随机生成）
- **`POST /v1/keystore/rotate-dek`**：body 空。DEK 轮转不需要预放文件

### 常见踩坑 / Common pitfalls
- **admin 无认证**：admin API（:8081）默认不鉴权，仅 bind `127.0.0.1`。暴露到网络前必须前置 reverse proxy 加 basic auth / mTLS / IP 白名单，否则同网任何人可 CRUD 所有凭据
- **CORS**：浏览器从其他 origin（`file://`、不同 host/port）发 `fetch()` 到 admin 会被跨域策略拦。绕开方法：(a) 起一个同源 reverse proxy 转发到 admin，(b) 敏感响应不要缓存（`Cache-Control: no-store`）
- **PUT/DELETE 不需要 `If-Match` header**（last-write-wins，外部并发不做乐观锁）
- **rotations 状态字段**：`/v1/keystore/status` 返回 `rotation_state ∈ {idle, swap_pending, reencrypting, ready_to_commit}`。`swap_pending` = KEK swap 已发起但未提交（`PendingKekVersion > 0`）；`reencrypting` = DEK 轮转重加密阶段，`straggler_count` 字段表示还有多少 api_key 未重加密；`ready_to_commit` = 重加密完成待提交（`straggler_count == 0`）
- **PID 文件**：`$config.data_dir/router.pid`（默认 `./data/router.pid`）。`cmd/credential-router/main.go:94`：`pidPath := filepath.Join(cfg.DataDir, "router.pid")`。Nohup 模式起进程后立刻写 pid 文件；重启前 `rm` 干净旧文件

### proxy 行为细节 / Proxy behavior
- **不跟随上游 3xx**：`http.Client.CheckRedirect`（`internal/proxy/handler.go:64-66`）固定返回 `http.ErrUseLastResponse`，proxy 不会替客户端做二次跳转；上游 3xx 响应（含 `Location`）原样透传。客户端需要处理 `3xx` 状态码或预拼接正确的 `api_base`
- **Hop-by-hop 头剥离**：转发时移除标准 `Connection` / `Keep-Alive` / `Proxy-Authenticate` / `Proxy-Authorization` / `Te` / `Trailer` / `Transfer-Encoding` / `Upgrade` + `Connection` 头里点名的附加头。同时剥客户端的 `Host` 头并替换为上游 URL 的 host
- **轮转 drain 轮询**：`keystore.Manager.WaitInflightDrained`（`internal/credmgr/keystore/snapshot_lifecycle.go:138-153`）每 50ms 检查一次 prev snapshot 是否还有 in-flight refCount，是固定常量，不配置
- **缓存 tombstone 上限**：cache 中同时存在的"已删除"墓碑条目有硬上限（`cache.TombstoneMax = 1000`，`internal/credmgr/cache/credential_cache.go:118-120`）。超出后 PutTombstone 直接返回错误，admin 客户端继续能 DELETE 但缓存层不再额外记账。重启即清零

## 调试与排错 / Debug & troubleshooting

- **看完整 HTTP 日志**：admin / proxy 都打结构化 JSON 日志到 stdout，含 `proxy_key` 前 8 位（不打印全 key）+ 上游 status + 耗时。设 `server.log_file` 同时写文件
- **看密钥状态**：`curl 127.0.0.1:8081/v1/keystore/status | jq`
- **看实时 pprof**：用 `-tags instrumented` build 启动后访问 `http://127.0.0.1:8081/debug/pprof/*`
- **轮转进度字段**：`rotation_state ∈ {idle, swap_pending, reencrypting, ready_to_commit}`；`reencrypting` 阶段 `straggler_count` 表示还有多少 api_key 未重加密
- **配置文件合法但 binary 起不来**：通常是 `data_dir` 写权限或 KEK 分片缺失，日志会指明失败项
- **配置字段校验**（`internal/platform/config.go` `Config.Validate()`）：两类检查，**不是统一 `< 0`**
  - **`nonNegative*`**（`< 0` 报错，`0` 合法）：intervals（`rotation.period` / `rotation.drain_timeout` / `rotation.complete_drain_timeout`）、TTL（`cache.tombstone_ttl` / `cache.entry_ttl` / `ssrf.cache_ttl` — 0 表示关闭 cache，CONFIG.md:51 文档化）、`server.shutdown_zero_budget`（0 = 立即 zero）、`server.idle_conn_timeout`（0 = no idle limit，net/http 文档化）、`upstream_timeout_ms`（0 = 无超时）
  - **`mustBePositive*`**（`<= 0` 报错）：loop 计数（`rotation.max_phase_a_loops` — 0 = 0 轮 = 没干活）、byte cap（`server.max_response_bytes` / `server.max_request_bytes`）、admin validation maxlen（5 个 — 0 = 拒绝全部）、cache 上限（`cache.max_entries` — 0 = 拒绝所有缓存）、keep 计数（`backup.keep` / `backup.key_snapshot.keep`）、mandatory timeout（`server.read_header_timeout` / `server.shutdown_timeout` / `ssrf.timeout`）、`recovery.max_wait`
  - **Load() 与 Validate() 分工**：Load() 替换 `<= 0` 为默认值（YAML 缺字段不报错）；Validate() 跑在 Load() 之后，捕获直接构造 / 绕过 Load 的非默认零值误配置。两层互补