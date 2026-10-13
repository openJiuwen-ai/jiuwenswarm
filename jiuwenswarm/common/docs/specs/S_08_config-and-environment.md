# 配置与环境（config / env / 模式归一）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_08 / config-and-environment |
| 关联模块 | jiuwenswarm/common/{config, config_provider, path_provider, local_env_config, hooks_config, http_proxy_config, kv_cache_affinity_config, mode_matrix, work_mode, context_keys}.py |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **配置加载链与环境变量双轨体系**：config.yaml 的读 / 合并 / `${VAR}` 解析 /
带锁写回，配置源与路径解析两个 SPI，双轨环境变量（Track A/B），hooks 段 schema，出站
代理，KV cache 亲和规则，运行模式归一。模型配置的校验与归因归 [[S_10]]；敏感值的存储
加密归 [[S_04]]。

## 装配关系（一句话）

入口是 `config.get_config()`：慢路径先向 `config_provider`（企业注入，fail-open 回退
Default）要配置源，否则合并 YAML 模板 + 用户 override 并经 `resolve_env_vars` 解析
`${VAR}`——变量值经 `local_env_config` 的 tip/overlay 读取，而 tip 写入反向失效 config
的 ns 缓存；`hooks_config` / `kv_cache_affinity_config` 是 config 树特定段的 schema /
规则消费者；`http_proxy_config` 消费 local_env_config 读代理；`mode_matrix` 组合
`work_mode` 并消费 config；`path_provider` 与 `context_keys` 相对独立。

## 不变量

- **N1 配置缓存双重键**：合并结果按 `(path, mtime_ns)` 缓存（省 ~150ms/次）；
  `get_config()` 按 `(sid, aid)` ns + overlay 内容 hash 做 TTL + 版本号缓存。
  **返回共享引用，消费方禁止原地修改**——要改走 `update_config` / `set_config`。
- **N2 写回三保险**：portalocker + 原子替换（tmp + os.replace）+ ruamel round-trip
  （保注释保格式）。绕过 `set_config` 直写文件会与锁协议冲突。
- **N3 `${VAR}` 解析**：支持 `${VAR}` / `${VAR:-def}` / `${VAR-def}`；Track A 键直读
  `os.environ`，其余走 tip（见 N5）；api_key / token 类自动解密（经
  ExtensionRegistry crypto，无 provider 明文直通）。
- **N4 配置目录可重定位**：`JIUWENSWARM_CONFIG_DIR`；测试隔离标配是改它 +
  `clear_config_cache()`。
- **N5 双轨环境变量**：Track A = spawn 键（`SPAWN_ENV_KEYS`）驻留 `os.environ`；
  Track B = 业务键仅存进程内 `(sid, aid)` tip bag（active ∪ staged，staged 胜）+ 任务
  overlay。**Track B 永不驻留 os.environ**；`export_agent_environ` 是子进程唯一出口
  （含加解密与 `JIUWENCLAW_*` ↔ `JIUWENSWARM_*` 别名互换）。
- **N6 overlay 绑定即 seal**：`bind_task_env_overlay` 后 miss 即 unset（不回退 tip）；
  `_UNBOUND` 哨兵区分"未绑定"与绑定 `{}`。overlay 是任务级 ContextVar，bind/reset
  必须成对。
- **N7 tip 联动失效**：tip 写入（`set_os_environ` 等）联动失效 config 的 ns 缓存。
  绕过 local_env_config 直写 `os.environ` 会破坏该联动（且 Track B 值根本不在那）。
- **N8 空串凭证键不覆盖**：API_KEY 等凭证键空串不覆盖已有值（防"手滑清空密钥"）。
- **N9 provider fail-open / 企业 MCP fail-closed**：`config_provider` 异常回退
  Default（fail-open）；但企业配置源下 MCP 段 loaded 为 None 时**清空本地 mcp**
  （fail-closed，防企业环境裸连本地 stdio）。
- **N10 hooks 段宽容解析**：schema 类型错误仅 warning 跳过该 matcher（fail-open）；
  `disable_all_hooks` 总开关；matcher 支持 `*` / `|` OR / `^…$`·`.*` 正则；17 个
  HookEvent 二分为 Rail 层与 Gateway 层（`is_rail_event` / `is_gateway_event`）。
- **N11 代理解析顺序与重试**：tip/overlay（无进程回退）→ `os.environ`（Windows 大小写
  不敏感已处理）；NO_PROXY 支持 `*` / 域名后缀 / IP / CIDR；`ProxyError` 自动去代理
  重试（fail-open）；requests 封装 `trust_env=False`（`REQUESTS_CA_BUNDLE` 需显式注入）；
  `JIUWENSWARM_SSL_VERIFY` 默认开启 TLS。
- **N12 affinity 不变量 fail-closed**：affinity 开启 ⇒ release 必 false 且默认 provider
  必为 `AscendAffinity`；请求级互斥冲突直接抛 ValueError。provider 判定链：
  `models.defaults[is_default]` → `models.default` → `react.model_client_config` →
  `MODEL_PROVIDER` env 兜底。
- **N13 模式三级解析**：Web 组合表（`mode` + `work_mode`）→ 新三段直解（**不经**
  legacy 归并，防语义漂移）→ `legacy_resolver` 回调；旧 canonical 经
  `DEPRECATION_MAP` 静默升级；**`deprecate_mode` 对 None / 空串原样返回**（铁律，
  禁降级为默认值）；`team+code` 归一 `code.team`。
- **N14 work_mode 正交**：work_mode（work 办公 / code 工程）与 mode 正交；Web 默认
  `work`、TUI 默认 `code`；虚拟默认项目 ID：work → `default`、code → `default_code`；
  `normalize_work_mode` 宽松（非法回落 default）仅限迁移/兜底，公开入口走严格校验。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `config.py`（~2500 行） | `get_config` / `get_config_raw` / `get_merged_config_dict` / `resolve_env_vars` / `set_config` / `update_config` / `clear_config_cache` / `CONFIG_YAML_PATH` | N1–N4；各配置段（permissions/models/mcp/teams/hooks）读写 API |
| `config_provider.py` | `ConfigProvider`（Protocol）/ `register/get/reset_config_provider` / `DefaultConfigProvider` / `AgentConfigContext/Result` / `apply_enterprise_mcp` | N9；配置源由 `JIUWENSWARM_CONFIG_SOURCE` 或 `config.source` 决定 |
| `path_provider.py` | `PathCategory`（~20 类目录）/ `PathProvider`（Protocol）/ `register/get/reset_path_provider` / `current_path_context` / `bind_path_session_id` | 进程级单例 provider；请求身份 task-local（ContextVar） |
| `local_env_config.py`（~1340 行） | `SPAWN_ENV_KEYS` / `BUSINESS_MIRROR_KEYS` / `get_local_config` / `read_env[_if_set]` / `set_os_environ` / `export_agent_environ` / `bind_agent_env_ns` / `bind_task_env_overlay` / `effective_tip` | N5–N8 |
| `hooks_config.py` | `HookEvent`（17）/ `HooksConfig.match` / `HookMatcher` / `load_hooks_config` / `is_rail_event` / `is_gateway_event` | N10 |
| `http_proxy_config.py` | `requests_get/post` / `resolve_requests_proxies` / `resolve_httpx_proxy` / `should_bypass_proxy` / `ssl_verify_enabled` | N11 |
| `kv_cache_affinity_config.py` | `ASCEND_AFFINITY_PROVIDER` / `get_default_model_provider` / `validate_affinity_invariant` / `normalize_affinity_request` / `KVC_CONFIG_KEYS` | N12 |
| `mode_matrix.py` | `resolve_request_mode` / `ResolvedMode` / `deprecate_mode` / `DEPRECATION_MAP` / `NEW_CANONICAL_MODES` / `WEB_COMPOSABLE_MODES` / `is_plan_mode` 等 | N13；8 种 canonical 三段 |
| `work_mode.py` | `SUPPORTED_WORK_MODES` / `normalize_work_mode` / `resolve_default_project_id` / `is_default_project_id` | N14 |
| `context_keys.py` | `JIUWENSWARM_CHANNEL_CONTEXT_KEY` | [[S_01]] N5 |

## 数据结构

- **tip**：`(session_id, agent_id)` → 生效环境 dict（active ∪ staged，staged 胜）。
- **overlay**：任务级密封 dict（ContextVar）；`effective_tip = tip ∪ overlay`（overlay
  键存在即覆盖，miss 即 unset）。
- **config 缓存**：`(path, mtime_ns)` → 合并 dict；`(sid, aid, overlay_hash)` → agent
  视图 + 版本号。
- **HooksConfig**：`{events: [{event, matchers}]}`；match 失败静默跳过。

## 消费者

`config`（40+ 文件，全仓最广之一）、`local_env_config`（77 文件）、`hooks_config`
（server/hooks、gateway/hooks、channels、agents rails）、`http_proxy_config`
（agents/harness tools）、`mode_matrix`（server/runtime、gateway/web、observability）、
`work_mode`（server/runtime/session、gateway/cron）、`kv_cache_affinity_config`
（config 持久化校验、gateway/web）。

## 修改检查单

- 改 config 缓存键 / 失效逻辑 → `test_config_parse_cache.py` +
  `test_config_concurrent_models.py` 全量回归；检查 tip 联动（N7）。
- 改 `${VAR}` 语义 → 三种形式（无默认 / `:-` / `-`）回归 + Track A/B 读取路径确认。
- 改 overlay seal 语义 → miss-unset 行为回归（这是防"幽灵配置"的关键）。
- 加 HookEvent → Rail/Gateway 归类 + matcher 语法回归 + hooks 消费方三处
  （user_hook_rail / gateway handler / extensions handler）确认。
- 改模式归一 → 组合表 / 直解 / legacy 三级矩阵 + `deprecate_mode` None/空串原样铁律 +
  [[S_03]] Mode 枚举同步。
- 改代理解析 → Windows 大小写 / NO_PROXY 四形态 / 去代理重试回归
  （`test_http_proxy_config.py`）。

## 与其它 spec 的关系

- [[S_04]]：敏感名加密通道（`legacy.py` / N3 解密）。
- [[S_10]]：模型段（models.defaults / react.model_client_config）的校验与归因消费。
- [[S_09]]：mcp 段的 schema 与准入消费。
- [[S_03]]：Mode 枚举终值。
