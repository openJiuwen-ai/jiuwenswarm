---
id: F_20261126_0957_enterprise-execution-guard
title: 企业级执行护栏改为独立模板槽位
type: F
author: hanjiahui
author_email: hanjiahui5@huawei.com
modules:
  - jiuwenswarm/enterprise
files:
  - jiuwenswarm/enterprise/config/config_provider.py
  - jiuwenswarm/enterprise/config/effective/apply_execution_guard.py
  - jiuwenswarm/enterprise/config/effective/apply_models.py
  - jiuwenswarm/enterprise/config/effective/db_queries.py
  - jiuwenswarm/enterprise/config/effective/execution_guard_body.py
  - jiuwenswarm/enterprise/config/effective/loader.py
  - jiuwenswarm/enterprise/config/effective/schemas.py
  - tests/unit_tests/enterprise/config/test_apply_execution_guard.py
  - tests/unit_tests/enterprise/config/test_apply_models.py
  - tests/unit_tests/enterprise/config/test_effective_loader_slots.py
designs: []
design_note: 2026-11-25 已确认的企业执行护栏配置方案。本仓没有对应 D 编号。
---

# 企业级执行护栏改为独立模板槽位

## 关联依据

- 设计：2026-11-25 已确认的企业执行护栏配置方案。本仓没有对应 D 编号。
- spec：本仓没有新增 execution_guard 规约。当前契约写在本记录和 `execution_guard_body.py` 的字段白名单里。
- 日期：2026-11-26。
- 实际实施范围：本仓企业配置加载与套用。个人版仍只读 `config.yaml`。Manager 建表、界面、Gateway 落库在其他仓库，本记录只写它们和本仓的交接，不记录那些仓库的实现。

## 决策归属与关系

新决定。此前企业模型套用会把模型行的 `retry_count` 写入异常护栏的 `transient_max_retries`，并强制打开该护栏。该路径已删除。护栏开关和 `transient_max_retries` 只来自执行护栏模板；未套用模板时沿用 `config.yaml` 的 `execution_guard`。

## 上下文与跨边界影响（按需）

产生方是 Manager 上的执行护栏模板。权威副本在 Gateway 库的 `execution_guard_template`；Agent 模板用槽位键 `execution_guard` 引用 `template_id`。本仓不调用 Gateway HTTP，而是用现有 Gateway DB 读取器按槽位取行。

消费者是企业版配置合并后的内存快照。`interface_deep` 与 `member_rails` 仍从 `execution_guard.model_anomaly_detection_rail` 读异常护栏；断路器仍只在 Deep Agent 上从 `execution_guard.circuit_breaker` 构造。算法、CUA 写死的 `ModelAnomalyDetectionRail()`、`config.yaml` 默认值都没有改。

查询失败、未绑定、行禁用或 body 非法时保留 YAML，不让这次加载失败。Gateway 写入不会清本仓缓存：模板行缓存默认 3600 秒，Agent 模板绑定缓存 600 秒，每个 AgentServer 进程一份。个人版在 `ConfigSource.YAML` 时不进入企业套用。

配套 Gateway 变更是 agent-gateway !365（检视时核对：表名 `execution_guard_template`，校验副本与本仓字段集一致，仅 docstring 措辞不同）。本仓可以先合入；表尚未创建时该槽位降级，其他槽位继续加载。

## 实际实现与开发决策

- 槽位键是本地字符串 `execution_guard`，表名 `execution_guard_template`。`e2a.template_slots` 还没有这个枚举。`normalize_template_ref` 会保留未知字符串键，接线方式与 react 相同，不改 e2a。
- 套用放在模型与 MCP 之后、凭据等其余企业策略之前。模型套用不再创建或修改 `execution_guard`。
- body 合法时深拷贝整份配置，用校验后的 body 整段替换 `execution_guard`，不写回 `config.yaml`。不与 YAML 做深度合并：模板是完整快照，避免 YAML 里多出来的键继续生效。react 的深度合并只适用于「模型槽位键从 react data 里丢掉」那种补丁，不用于本槽位。
- 多个模板只取第一个，并打 warning。行上 `enabled is False` 才视为禁用并保留 YAML；`None`、`0` 和其他真值视为启用，避免 MariaDB `tinyint` 1 被当成关闭。非法 body 打 error（带 `exc_info`）后保留 YAML，不把护栏构造器变成返回空。
- 校验是字段白名单，缺字段和未知键都拒绝。整数拒绝布尔。`repeat_window_chars` 不得低于 `max(repeat_max_pattern_chars * repeat_min_count, repeat_min_total_chars, single_char_repeat_count)`。`critical_threshold` 不得小于 `warning_threshold`。默认值与当前 `config.yaml` 相同；YAML 没有 `transient_max_retries`，默认 3，与构造器缺省一致。`transient_base_delay_seconds`、`backoff_seconds` 不进入模板。
- `retry_count` 仍写入 `model_client_config.max_retries`，因为模型行上该列还在。SDK 重试仍固定为 0，没有任何护栏读取这个 `max_retries`。不再用它改 `transient_max_retries`，也不再强制打开异常护栏。未绑定护栏模板时异常护栏保持 YAML 的 `enabled: true`，断路器保持 `enabled: false`。

## 与设计的偏差

无。护栏不再由模型重试次数驱动，与设计一致。本仓继续把行上的 `retry_count` 抄进 `model_client_config.max_retries` 只是兼容旧列，不改变护栏结果。

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 / 复现步骤 / 记录链接 | 实际结果 | 未覆盖项 / 失败原因 |
| --- | --- | --- | --- | --- |
| 2026-11-25 本仓三个单测文件 | 检视工作区，带 conftest | `pytest tests/unit_tests/enterprise/config/test_apply_execution_guard.py tests/unit_tests/enterprise/config/test_apply_models.py tests/unit_tests/enterprise/config/test_effective_loader_slots.py` | 17 passed | 当时还没有正向槽位用例 |
| 2026-11-25 槽位加载 | 本机 Windows，`jiuwenswarm/.venv` | `python -m pytest tests/unit_tests/enterprise/config/test_effective_loader_slots.py -o addopts=` | 6 passed | 只覆盖这个文件 |
| 2026-11-25 企业环境对话 | 命名空间 `hjh`，Gateway 库 `gateway_hjh`，AgentServer `gateway.log` | 模板 `222` 断路器关闭，再改绑模板 `333` 并打开断路器后新对话 | 日志先后出现 `breaker_enabled=False`、`breaker_enabled=True` | 同一对话会打多条 applied，因为创建实例、模型预热、MCP 加载都会走套用；不表示每次都查库 |

套用测试覆盖了未绑定保留 YAML、合法 body 整段替换且不写文件、非法 body 回落、禁用行、多模板取第一个，以及护栏套用晚于模型套用。正向加载测试要求 `fetch_templates_by_slot` 以 `execution_guard` 被调用，并把该行放到 `result.execution_guard`。降级测试要求表查询失败时该槽位为空、MCP 仍在。

### 可见行为、副作用与证据有效范围

用户可见结果是企业版对话使用的护栏开关和重试次数。未绑定、禁用或 body 非法时与只改 `config.yaml` 相同。绑定并下发成功时，以 Gateway 行的 body 为准。

上述集群证据只说明当时那次对话读到了模板 `222` 和后来的 `333`。它不覆盖缓存过期前的旧绑定、多副本 AgentServer 各持一份缓存，也不覆盖个人版。单测使用假 DB，不证明 Gateway 表结构。

当前本仓实现版本是 `b32d73831`。检视记录的 17 passed 对应该提交之前的树；正向槽位用例在其后补上，并由本机 6 passed 覆盖加载文件。

## 验证结论

- 已证明范围：合法 body 会替换内存中的 `execution_guard` 且不写 YAML；未绑定、禁用、非法 body、查询失败都保留 YAML 或跳过该槽位。槽位字符串能进入加载。企业环境一次改绑后，新对话日志中的断路器开关与库中模板一致。
- 未覆盖或未通过的承诺：三份校验器（本仓、Gateway、Manager）没有跨仓 CI。缓存失效和多副本不一致没有自动化用例。
- 可支持的交付判断：本仓加载与套用符合 2026-11-25 设计。Gateway 表未就绪时本仓空转降级，闭环要等 Gateway 建表和 Manager 下发。

### 覆盖与资料同步

已核对套用顺序、body 白名单、槽位接线、模型套用不再改护栏，以及上述测试和一次集群日志。没有更新 spec，也不把本次验证回填进设计。

## 已知遗留

| 遗留项 | 未处理原因 / 性质 | 影响与现有处置 | 后续入口 / 条件及所需验证 |
| --- | --- | --- | --- |
| 校验器三份手写副本 | 有意分仓，Manager / Gateway / 本仓各有一份 | 字段集漂移时非法模板会在本仓回落 YAML，表现为配了不生效 | Gateway !365 与 Manager 副本变更时对照 `execution_guard_body.py` |
| Gateway 写入不清 AgentServer 缓存 | 沿用现有模板缓存，不在本次新建失效通道 | 绑定最多约 10 分钟、已缓存 body 最多约 1 小时仍用旧值；多副本各自缓存 | 需要更短生效时重启对应 AgentServer，或以后做主动失效 |
| 模型行 `retry_count` 仍写入 `max_retries` | 列保留，SDK 固定不重试 | 该值不驱动护栏；模型页不应再把它当作护栏重试次数 | Manager 侧字段下线由 Manager 仓负责 |

## 实施演进与修订（发生时填写）

### 2026-11-25 补执行护栏槽位的正向加载测试

- 原结论与适用范围：首版只断言查询失败时 `result.execution_guard is None`。
- 新证据与原因：槽位被丢掉时该断言也会通过，检视要求证明 `execution_guard` 真的进入了查询。
- 最终变化：增加 `test_load_execution_guard_slot_returns_template_row`，断言以该槽位调用查询并取回模板行。
- 旧结论的效力：降级断言仍然有效，只覆盖失败路径。
- 影响与同步：只增加测试，不改变套用语义。
- 验证：本机该文件 6 passed。
