# 模型与推理参数

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_10 / model-and-reasoning |
| 关联模块 | jiuwenswarm/common/{model_client_extensions, model_config_validation, model_identity, openrouter_attribution, reasoning_config, reasoning_injector}.py |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **模型配置的装载与校验、模型身份指纹、厂商归因注入、reasoning_level 参数化**。
这组模块全部处于"模型配置 → 请求构造"段，不触碰实际 HTTP 调用。thinking 开关管线
（""/default/off/on）归 [[S_06]]；模型配置段在 config.yaml 的位置归 [[S_08]]。

## 不变量

- **N1 外部 client 装载白名单**：env `AGENT_EXTRA_MODEL_CLIENTS`（逗号分隔，须
  `isidentifier`）；搜索根 = env `EXTENSION_DIRS`（`;` 分隔）+ 内置
  `jiuwenswarm/extensions/model_clients/<name>.py`；模块名带路径哈希防冲突；**导入失败
  回滚**已注册 client 并移出 `sys.modules`；RLock 防重入。装配点：`app_gateway` /
  `app_agentserver` 启动（[[S_01]] N6）。
- **N2 占位符 api_base 黑名单**：`is_placeholder_api_base` 判定文档示例值——精确 URL
  `https://example.com/compatible-mode/v1` + example.com/org/net 及其子域。**宁可误拒
  不可放行**：占位配置进生产会以神秘 404 失败。
- **N3 身份指纹确定性**：身份 = `sha256(api_base 去尾斜杠 + model_name + provider
  小写)`，前缀 `model-identity-v1:`；凭证指纹 = `sha256(api_key + custom_headers)`（header
  名归一小写）。`normalize_model_identity_reference` 严格校验前缀 + 64 位 hex。指纹是
  relay 与 team 路由跨组件指代同一模型的**无凭证**通道——api_key 绝不进指纹。
- **N4 OpenRouter 归因窄注入**：仅 `client_provider == "openrouter"` 注入
  `HTTP-Referer: https://openjiuwen.com/` / `X-OpenRouter-Title: JiuwenSwarm` /
  `X-OpenRouter-Categories`；`custom_headers` 用 `setdefault` **不覆盖用户值**；
  `inject_attribution_to_config` 覆盖 `models.defaults`(list) / `models.default` /
  `react` 三处。
- **N5 reasoning 三层白名单**：provider ∈ {openai, deepseek, dashscope}；host 判定
  `deepseek_official`（api.deepseek.com）/ `dashscope_bailian`（dashscope +
  compatible-mode）；模型仅 deepseek-v4-pro / flash / flash-0731
  （`SUPPORTED_DEEPSEEK_V4_MODELS`）。三层全过才注入。
- **N6 reasoning_level 是内部字段**：注入前从 runtime config **剥离**、绝不透传 SDK；
  level 归一 off / low / medium / high，effort 映射 low/medium/high → high。
- **N7 厂商参数形态**：deepseek 官方 → `extra_body.thinking = {type: ...}`；百炼 →
  `extra_body.enable_thinking = bool`；映射为 high 时补 `reasoning_effort = high`；
  非白名单目标原样返回（不注入）。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `model_client_extensions.py` | `load_extra_model_clients` | N1 |
| `model_config_validation.py` | `is_placeholder_api_base` / `PLACEHOLDER_API_BASES` / `EXAMPLE_DOMAINS` | N2 |
| `model_identity.py` | `build_model_identity_reference` / `normalize_model_identity_reference` / `build_model_credential_fingerprint` / `MODEL_IDENTITY_REFERENCE_PREFIX` | N3 |
| `openrouter_attribution.py` | `OPENROUTER_ATTRIBUTION_HEADERS` / `inject_attribution_headers` / `inject_attribution_to_config` / `is_openrouter_provider` | N4 |
| `reasoning_config.py` | `resolve_reasoning_target` / `resolve_reasoning_provider_kind` / `normalize_reasoning_level` / `LEVEL_MAPPING` / `SUPPORTED_DEEPSEEK_V4_MODELS` / `OPENAI_SDK_REASONING_PROVIDERS` | N5 / N6；纯函数层，唯一直接消费方是 reasoning_injector |
| `reasoning_injector.py` | `inject_reasoning_params` / `build_reasoning_model_request_kwargs` / `inject_deepseek_official_payload` / `inject_dashscope_bailian_payload` | N7 |

## 执行路径 / 消费者

- `interface_deep.py` 是模型侧消费最集中入口：占位校验（N2）、归因注入（N4）、
  reasoning 注入（N7）。
- reasoning_injector 另消费：`agents/harness`（team_manager、team_runtime_inheritance、
  evolution_rails、wiki_tools）、`gateway`（im_inbound / im_outbound、web/tui handlers）、
  `server`（kv_cache_affinity_lifecycle、im_hosting/gate）。
- `model_identity` 消费：`agents/harness/team/config_loader`、
  `server/runtime/team_entity_store`。
- `model_client_extensions` 消费：`gateway/app_gateway`、`server/app_agentserver`
  （启动装载）+ `tests/test_extra_model_clients.py`。

## 边界 case 清单

| 场景 | 现行为 | 测试 |
|---|---|---|
| env 模块名非法 / 文件不存在 | 装载拒绝，已注册部分回滚 | `test_extra_model_clients.py` |
| api_base 为文档示例值 | 校验拒绝（N2） | `test_model_config_validation.py` |
| 用户已设 HTTP-Referer | setdefault 不覆盖 | 归因测试 |
| 非 openrouter provider | 不注入任何头 | 同上 |
| deepseek-v3.x + thinking | 白名单外，原样返回 | reasoning 测试 |
| reasoning_level 透传到 SDK | 注入前剥离（N6） | reasoning 测试 |

## 修改检查单

- 扩 reasoning 白名单（新 provider / host / 模型）→ 三层判定矩阵回归 + 参数形态映射
  （N7）成对扩；禁止只扩判定不扩注入。
- 改指纹公式 → 破坏性变更：relay / team_entity_store 存量引用失效，必须给迁移路径。
- 改占位黑名单 → example 域族完整性检查（org/net 子域）。
- 新增归因头 → 与 OpenRouter 文档核对，setdefault 语义保持。

## 与其它 spec 的关系

- [[S_06]]：thinking 管线（正交，见其范围段）。
- [[S_08]]：models 段与 react 段的配置来源。
- [[S_01]]：N1 装配点登记。
