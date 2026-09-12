# 配置参考

本文档列出 binary 接受的所有配置项。最小可跑集见 `config.example.yaml`；下方的字段是高级调优，默认值已为 appliance 部署调好，部署需要不同时可以覆盖。

## 必填 / Required

binary 缺这些字段会拒绝启动。

| 字段 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `server.bind_address` | `host:port` | `127.0.0.1:8080` | Proxy 监听地址。 |
| `server.external_address` | `http(s)://host[:port]` | (empty) | 当 `bind_address` 是 wildcard (`0.0.0.0` / `::`) 时**必填**——admin POST `/v1/credentials` 响应里的 `proxy_address` 字段用此值（binary 不知道 wildcard 之后的真实客户端 endpoint）。非 wildcard 时可省；非 http(s) URL 会拒绝启动。 |
| `admin.addr` | `host:port` | `127.0.0.1:8081` | Admin API 监听地址。 |
| `data_dir` | path | `./data` | 存放凭据 DB 和 secrets 子目录（S1 分片、crypto_mode）。缺失则自动创建。 |
| `rotation.period` | duration | `720h` | 自动轮转 ticker 触发周期。启动本身只跑 *convergence*（恢复上次运行未完成的轮转——fresh install 是 no-op），不触发新轮转。第一次自动轮转在启动后 `period` 触发；fresh install 时 `StartAutoRotate` 内部那次调用是 gate-satisfied no-op，因为 `SelfInit` 把 `dek_rotated_at` 设为 `now`。 |

## 可选 — SSRF 策略 / Optional — SSRF policy

把 proxy 暴露到 appliance 信任域之外时调整这些。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ssrf.dial_check` | bool | `false` | 开启 outbound dialer SSRF 防护。默认关闭，因为产品以 appliance 部署，所有推理走内部网络；非 appliance 部署需开启。 |
| `ssrf.allowed_hosts` | []string | `[]` | 同时作用于 proxy dialer 和 admin 的 `real_url` 校验的主机白名单。空 = 无白名单。设置后只接受列表中的 hostname，支持通配符 `*.foo.com`。 |

## 高级 / Advanced

其他所有配置项。默认值已为 appliance 场景调好，只有部署需要不同时才调整。

### Crypto

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `crypto_mode` | string | `aes` | 密钥加密算法。`aes` = AES-128-GCM，`sm4` = SM4-GCM（国密）。仅在首次启动 SelfInit 时从 config 读取并写入磁盘文件；此后磁盘文件为准（不可变），config 值忽略。切换算法需删除 `data_dir/secrets/crypto_mode` + `data_dir/credentials.db` 重新初始化。 |

### Server

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `server.max_response_bytes` | int64 | `10485760`（10 MB） | 上游响应体大小硬上限。模型返回大 completion 时调高。 |
| `server.max_request_bytes` | int64 | `10485760`（10 MB） | 客户端请求体大小硬上限。 |
| `server.shutdown_zero_budget` | duration | `5s` | shutdown 时清零内存中 key material 的时间预算。并发 in-flight holder 多时调高。 |
| `server.read_header_timeout` | duration | `10s` | HTTP listener 的 DoS 防护。 |
| `server.shutdown_timeout` | duration | `10s` | graceful-shutdown 总超时。 |
| `server.idle_conn_timeout` | duration | `90s` | 出站上游连接的 keep-alive idle 超时。 |
| `server.log_file` | path | (empty) | 如果设置，结构化 JSON 日志同时写入此文件（append 模式，0o644）。空 = 仅 stdout。 |

### Upstream

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `upstream_timeout_ms` | int | `30000` | 每个 proxy 出站请求的超时。延迟敏感路径调低，慢模型 API 调高。 |

### SSRF（高级调优）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `ssrf.cache_ttl` | duration | `30s` | SSRF 检查的 DNS 解析缓存 TTL。设为 `0` 关闭缓存（消除 check 和 dial 之间的 TOCTOU 窗口）。 |
| `ssrf.timeout` | duration | `5s` | 单次 resolve 超时。 |

### Rotation

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rotation.drain_timeout` | duration | `5m` | 轮转在 Phase A 开始前等待 in-flight 请求完成的最长时间。有超过 5 min 的请求时调高。 |
| `rotation.max_phase_a_loops` | int | `100` | convergence 循环次数安全上限。重加密事务分摊到多次循环以保持 credentials 表写锁时间短。 |
| `rotation.complete_drain_timeout` | duration | `30s` | Phase A 最终硬截断——单轮 drain 超过这个值则 Phase A 中止。 |

### Cache

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `cache.max_entries` | int | `10000` | 凭据缓存最大条目数。多租户部署调高，内存紧张调低。 |
| `cache.tombstone_ttl` | duration | `1h` | 缓存记住已删除凭据的时间（用于快速返回 404）。 |
| `cache.entry_ttl` | duration | `10m` | 一次成功的凭据查找在缓存中的保留时间。 |

### Admin validation

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `admin.validation.user_id_max_len` | int | `256` | 凭据 create/replace 时 `user_id` 字段的最大长度。 |
| `admin.validation.real_url_max_len` | int | `2048` | `real_url` 最大长度。 |
| `admin.validation.key_tag_max_len` | int | `64` | `key_tag` 最大长度。 |
| `admin.validation.api_key_max_len` | int | `8192` | API key 值的最大长度。异常长的 key 调高。 |
| `admin.validation.auth_type_max_len` | int | `16` | `auth_type` 最大长度。 |

### Backup

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `backup_dir` | path | `./backups` | `data_dir` 的同级目录，存放全量备份 + key snapshot。缺失则自动创建。 |
| `backup.keep` | int | `3` | DB 备份文件保留数量（对 KEK 和 DEK 备份各保留 N 份，超出的最旧文件自动删除）。 |
| `backup.filename_template` | string | `backup-{type}-{ts}.db` | 文件名模板。`{type}` 展开为 `kek` 或 `dek`；`{ts}` 为毫秒时间戳。 |
| `backup.key_snapshot.enabled` | bool | `true` | 是否在每次备份时同时写 key snapshot。 |
| `backup.key_snapshot.filename_template` | string | `key-snapshot-{ts}.bin` | key snapshot 文件名模板。 |
| `backup.key_snapshot.keep` | int | `5` | key snapshot 保留数量（独立于 `backup.keep`）。 |

### Recovery

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `recovery.max_wait` | duration | `5m` | 启动 recovery 阶段的最长等待。超过这个时间 binary 拒绝启动。 |

## 硬编码（不可配置） / Hardcoded (not configurable)

这些是平台不变量，写在源代码里而非配置，避免跨部署漂移。

| 名称 | 值 | 位置 | 原因 |
|---|---|---|---|
| `keystore.MaxRowsPerTx` | `1000` | `internal/credmgr/keystore/rotation.go` | Phase A 单事务行数上限。事务必须短，避免持有 credentials 表写锁超过轮转 drain 窗口。 |
| `store.DefaultProxyMaxConns` | `32` | `internal/credmgr/store/store.go` | Proxy 热读的 SQLite 连接池大小。 |
| `store.DefaultAdminMaxConns` | `2` | `internal/credmgr/store/store.go` | Admin 写 + 轮转事务的 SQLite 连接池大小。 |
| `cache.TombstoneMax` | `1000` | `internal/credmgr/cache/credential_cache.go` | 缓存中允许的 tombstone（已删除条目标记）最大数量，避免删除风暴下 tombstone 无限增长。超出时 PutTombstone 报错但 DELETE 仍然成功（下次查询穿透 DB）。 |
| `WaitInflightDrained poll interval` | `50ms` | `internal/credmgr/keystore/snapshot_lifecycle.go` | 轮转 Phase B 等待 in-flight CRUD 释放 prev snapshot 的固定轮询间隔。 |