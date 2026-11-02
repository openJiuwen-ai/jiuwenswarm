# 传输安全（security）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / security-transport |
| 关联模块 | jiuwenswarm/common/security/（3 文件）+ ws_limits.py + ws_diagnostics.py |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **链路传输安全**：服务间 mTLS（link_mtls）、WebSocket Origin 白名单
（ws_origin）、加密提供方协议（base_crypto）、WS 消息大小限额（ws_limits）与 WS 异常
诊断（ws_diagnostics）。静态敏感值的存储加密归 [[S_04]]；MCP 出站的 SSRF 防护归
[[S_09]]；HTTP 出站代理归 [[S_08]]。

## 不变量

- **N1 link_mtls 三态**：`off`（默认，零影响）/ `observe`（预检失败仅告警，fail-open）/
  `enforce`（**全链 fail-closed**：启动预检失败直接抛错拒绝启动）。模式由 env
  `JIUWENSWARM_LINK_MTLS_MODE` 决定。`LinkMTLSConfig` 为 frozen dataclass。
- **N2 enforce 下防 header-only 反模式**：`validate_incoming_binding` 在 enforce 态对
  缺失/不符的绑定直接抛错——不做"有 cert 就行"的宽判；`binding_headers` /
  `cert_fingerprint` 是绑定校验的输入。
- **N3 Origin 白名单默认关闭**：总开关 `JIUWENSWARM_ENABLE_ORIGIN_CHECK` **严格
  `== "1"`** 才启用（"true"/"yes" 不启用）；白名单
  `JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS`；企业版 `is_enterprise()` 短路放行；
  **无 Origin 请求需显式哨兵 `"none"`** 才放行；hostname 小写匹配、不分 scheme/port。
- **N4 CryptoProvider 的实际容器是 ExtensionRegistry**：`base_crypto.py` 的模块级
  set/get_crypto_provider **无调用者**（保留 API）；生产上 crypto provider 挂在
  extensions 的 ExtensionRegistry，由 [[S_08]] local_env_config 的敏感名通道消费。
  改协议签名（encrypt/decrypt 字符串进出）必须两处同步。
- **N5 WS 消息限额双轨**：Gateway↔AgentServer 内部链 `AGENT_WS_MAX_MESSAGE_BYTES`
  （8 MiB）+ 发送预算 `AGENT_WS_SEND_BUDGET_BYTES`（6 MiB）；浏览器↔WebChannel 链
  `WEB_WS_MAX_MESSAGE_BYTES`（100 MiB，为 document.persist 的 base64+JSON 开销留余量）。
  内部链收紧是**有意设计**（流式事件、无大 base64），勿"统一"两轨。
- **N6 诊断鸭子探测**：`ws_diagnostics` 全部用 `getattr` 探测、不依赖具体 ws 库类
  （版本容错）；Enum 值归一化为原生值；close code 优先 exc 自身、其次 rcvd、再 sent；
  输出稳定 `key=value` 单行格式（日志解析契约）。
- **N7 link_mtls 依赖 SDK**：`link_mtls.py` 依赖 openjiuwen_runtime SDK 的预检能力
  （与 [[S_02]] N11 的"e2a 零 SDK 依赖"形成对照——security 子包允许 SDK 依赖）。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `link_mtls.py` | `LinkMTLSConfig.from_env`、`client_kwargs` / `uvicorn_ssl_kwargs`、`authorize_request`、`binding_headers`、`cert_fingerprint`、`validate_incoming_binding` | N1 / N2 |
| `ws_origin.py` | Origin 白名单校验（legacy / new websockets API 双兼容） | N3 |
| `base_crypto.py` | `CryptoProvider`（Protocol：encrypt/decrypt 字符串进出） | N4 |
| `ws_limits.py` | `AGENT_WS_MAX_MESSAGE_BYTES` / `AGENT_WS_SEND_BUDGET_BYTES` / `WEB_WS_MAX_MESSAGE_BYTES` | N5 |
| `ws_diagnostics.py` | `describe_ws_exception` / `describe_ws_peer` / `format_ws_diagnostics` | N6 |

## 执行路径

- link_mtls 消费：`web_ws_transport`、`agent_ws_server`、`trajectory_http`、
  `http_agent_client`、`app_agentserver`、`agent_http_server`、`link_binding_state`。
- ws_origin 消费：Web WS 接入层（channels/web 与 gateway 侧 transport）。
- ws_limits 消费：`gateway/routing/agent_client`、`server/ws_send`（发送预算）、
  `web_ws_transport`。
- ws_diagnostics 消费：`app_web`、`agent_client`、`app_gateway`、`web_ws_transport`、
  `web_rpc_host`。

## 边界 case 清单

| 场景 | 现行为 | 风险 / 备注 |
|---|---|---|
| enforce 态预检失败 | 启动抛错（fail-closed） | 部署漏配时服务起不来——这是设计意图，告警不是 |
| observe 态预检失败 | 仅告警，链路照常 | 用于灰度观察；升 enforce 前先跑 observe |
| 无 Origin 的 WS 请求 | 需白名单显式含 `"none"` | 非"缺省放行"；curl/原生客户端接入要显式登记 |
| 企业版 Origin 检查 | 短路放行 | 企业侧有独立接入层时才允许 |
| WS close code 缺失 | 依次回退 rcvd → sent → 无 | 诊断字段可能为 None，日志侧容忍 |

## 修改检查单

- 改 mTLS 模式语义 → 三态矩阵（off/observe/enforce × 预检成败 × 请求校验）全量回归
  （`tests/unit_tests/common/test_link_mtls.py` + 集成
  `tests/integration/test_link_mtls_http_sse_e2e.py`）。
- 改 Origin 判定 → `"1"` 严格开关 / `"none"` 哨兵 / 大小写 / 企业短路四个点逐一确认。
- 调 WS 限额 → 双轨（内部 8MiB / 浏览器 100MiB）分别评估；超限拒绝路径的前端提示同步。
- 改诊断输出格式 → `tests/unit_tests/common/test_ws_diagnostics.py` + 日志采集侧解析
  规则同步（key=value 是解析契约）。

## 与其它 spec 的关系

- [[S_04]]：静态敏感值加密（本 spec 只管链路）。
- [[S_08]]：`http_proxy_config` 的出站 TLS（`JIUWENSWARM_SSL_VERIFY`）与
  `REQUESTS_CA_BUNDLE` 注入语义。
- [[S_09]]：MCP 出站 SSRF 屏蔽（内网/元数据地址）。
