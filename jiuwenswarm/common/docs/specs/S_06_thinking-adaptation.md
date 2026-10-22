# thinking 参数适配管线

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / thinking-adaptation |
| 关联模块 | jiuwenswarm/common/thinking/（6 文件） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **thinking 参数（"深度思考"开关）从用户输入到模型调用 kwargs 的适配链**：
类型冻结 → 厂商匹配 → 降级适配 → rail 注入 → 钩子注册。`reasoning_level`（off/low/
medium/high 的推理力度）是**另一条独立管线**，归 [[S_10]]；两者勿混——thinking 管
"开关与风格"，reasoning 管"力度参数化"。

```
用户 thinking 值（""/default/off/on）
  → types.normalize_thinking          （非法回退 default）
  → vendor_map.match_vendor_style     （模型名 → 厂商风格，有序窄白名单）
  → adapter.adapt_thinking            （永不抛错；产出 ThinkingProfile(frozen)）
  → rail.ThinkingInjectRail.before_model_call （注入 extra["llm_call_kwargs"]，thaw 副本）
  → register_hook.register_thinking_hook（进程级注册进 openjiuwen TaskTool）
```

## 不变量

- **N1 合法值域冻结**：`THINKING_VALUES = frozenset({"", "default", "off", "on"})`；
  `normalize_thinking` 对非法值回退 `default`（不抛错、不静默改语义）。
- **N2 profile 冻结**：`ThinkingProfile` frozen dataclass（thinking /
  llm_call_kwargs / injected / degraded / reason / vendor_style / model_name）；
  `freeze_llm_call_kwargs` 深拷贝后冻结，`thaw_llm_call_kwargs` 解冻出可变副本——
  **注入模型调用时必须用 thaw 副本**（KV-cache 友好 + 不污染冻结源）。
- **N3 厂商白名单有序窄匹配**：`_VENDOR_PATTERNS` 有序表——GLM-5 / 5.1 / 5.2 精确窄
  匹配 + DeepSeek 关键字 → `extra_body_thinking_type` 风格；Qwen 的
  `extra_body_enable_thinking` 预留未启用。`match_vendor_style` 未命中返回 None；
  `style_to_kwargs` 遇未知 style **抛 ValueError**（表内穷尽，禁宽匹配）。
- **N4 adapt_thinking 永不抛异常**：invalid_value / unsupported_model / adapter_error
  三种情况统一降级，`degraded=True` + `reason` 可追溯。**降级不是错误**——thinking 是
  增强项，失败不得影响主调用。
- **N5 rail 注入点唯一**：`ThinkingInjectRail(DeepAgentRail)` priority=15，仅
  `before_model_call` 注入 `extra["llm_call_kwargs"]`；注入失败仅 warning（fail-open）。
- **N6 钩子幂等 + ImportError 降级**：`register_thinking_hook` 幂等注册进 openjiuwen
  core TaskTool；SDK 不可用时降级 warning（社区环境不装 SDK 也能跑）。
- **N7 日志摘要脱敏**：`kwargs_digest` 输出日志摘要而非全量 kwargs（防敏感参数进日志）。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `types.py` | `THINKING_VALUES`、`ThinkingProfile`、`normalize_thinking`、`freeze/thaw_llm_call_kwargs`、`kwargs_digest` | N1 / N2 / N7 |
| `vendor_map.py` | `_VENDOR_PATTERNS`、`match_vendor_style`、`style_to_kwargs` | N3 |
| `adapter.py` | `adapt_thinking` | N4 |
| `rail.py` | `ThinkingInjectRail` | N5 |
| `register_hook.py` | `register_thinking_hook` | N6 |

## 执行路径 / 装配点

- 进程注册：`server/app_agentserver.py` 启动时 `register_thinking_hook`（[[S_01]]
  装配表）。
- 请求内消费：`app_agentserver`（启动注册）、`server/runtime/skill_turbo` 的
  executor / thinking_seam（加速通道复用同一适配语义）、`agents/harness` 的
  `interface_deep.py`（rewrite fast path 强制 off）。
- 与 [[S_10]] reasoning 的分界：reasoning 判定（`resolve_reasoning_target`）在
  interface_deep 构造模型请求 kwargs 时独立执行，不经本 rail。

## 边界 case 清单

| 场景 | 现行为 | 测试覆盖 |
|---|---|---|
| thinking 非法值（如 "true"） | 回退 default | `test_thinking_adapter.py` |
| 模型不在白名单（如 GLM-4） | unsupported_model 降级，不注入 | `test_thinking_adapter.py` |
| adapter 内部异常 | degraded + reason，主调用继续 | `test_thinking_adapter.py` |
| SDK TaskTool 缺失 | 注册降级 warning | `test_thinking_register_hook.py` |
| 注入后 kwargs 被并发修改 | thaw 副本隔离冻结源 | `test_thinking_inject_rail.py` |

## 修改检查单

- 新增厂商风格 → 扩 `_VENDOR_PATTERNS`（**窄匹配**，精确版本号/关键字）+ `style_to_kwargs`
  表 + 三份测试文件回归；禁止前缀宽匹配（GLM-4 误吃 GLM-5 参数是历史教训）。
- 改 rail 优先级 → 评估与其它 rail（权限 / 观测）的顺序依赖，priority 15 的相对位置
  是契约。
- 改 profile 字段 → 冻结语义（N2）+ skill_turbo thinking_seam 的消费同步 + 序列化
  兼容（profile 会进日志/trace）。

## 与其它 spec 的关系

- [[S_10]]：reasoning_level 管线（off/low/medium/high 力度参数化），与本 spec 正交。
- [[S_01]]：N6 装配点登记（app_agentserver）。
