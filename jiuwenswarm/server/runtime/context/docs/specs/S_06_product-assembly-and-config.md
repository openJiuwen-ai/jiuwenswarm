# 产品装配、配置与补丁

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / product-assembly-and-config |
| 关联模块 | interface_deep / interface_code / interface_flash + context_read_patch + config.yaml |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

- 三适配器 **interface_deep / code / flash** 对 context rails、overflow recovery、config 的消费差异。
- **`react.context_engine_config`** 与 `_config_cache` 形状（agent 模式 = react 段）。
- **`context_read_patch`** 安装时机与语义。
- **ContextAssembleRail** 与 **ProgressiveToolRail.eager_tools** allowlist 同步。
- **`get_context_engine_enabled` 与 deep adapter 的双默认漂移**（已知遗留，改默认须两端对齐）。

### 本规约不管什么

- AgentManager 选 adapter 的 mode 解析 → server runtime specs。
- 全局 `react` 非 context 键（skill、evolution 等）→ skill 等姊妹模块。

## 不变量

- **N1（双默认漂移）**：`interface_deep._update_agent_rails` 使用 `context_engine_config.enabled` **缺省 False**；`team_runtime_inheritance.get_context_engine_enabled` 缺省 **True**。Good：文档与 issue 明确写调用路径。Bad：假设全仓统一默认。消除漂移时**必须**同一 PR 改两处并更新本 N1。
- **N2（Allowlist 同步）**：Progressive 工具变更 eager 列表后须调 `_sync_context_assemble_tool_allowlist`，使 Assemble 的 `# 可用工具` 段与固定 schema 一致；见 `test_context_assemble_allowlist_compat.py`。
- **N3（读 patch 只读）**：`apply_context_read_patch` 在 deep `create_instance` 链安装；patch 仅在人设/ context section 读路径设 `only_read=True`，**写路径不改**；重复 apply 无效果。
- **N4（Flash 裁剪）**：flash `_FLASH_RAIL_KEEP` **不含** `_context_overflow_recovery_rail`；`_update_rails_for_mode` 卸载白名单外动态 rail——长上下文容错弱于 agent（interface_flash 头部说明）。
- **N5（Code 复用 deep 构建）**：`interface_code._build_context_processor_rail` 委托 `interface_deep._build_context_processor_rail(self._config_cache)`；改 processor 构建只改一处实现。

## 接口契约

### 模式 × Context 能力（结构表）

| 能力 | deep (agent) | code | flash |
|---|---|---|---|
| ContextAssembleRail | `_update_agent_rails` 注册 | 固定 rail 表 | 白名单内则保留 |
| ContextProcessorRail | `enabled` 门控 | 同 deep | 白名单内则保留 |
| ContextOverflowRecoveryRail | 装配 attempts=3 | 随 deep 父类 | **不装配** |
| context_read_patch | apply on create | 继承 deep | 继承 deep |
| `_deep_agent_context_engine_config` | create_deep_agent | 继承 | 继承（flash 合并 react override） |

### config 根：`jiuwenswarm/resources/config.yaml`

路径：`react.context_engine_config`（注释为产品契约，改键必 sync specs）。

关键子键见 [[S_02]] / [[S_04]]；`enabled` 为 ProcessorRail 总开关（Assemble 除外，[[S_03]] N1）。

### context_read_patch

```python
apply_context_read_patch()   # 生产 create_instance
remove_context_read_patch()  # 单测 teardown
```

补丁点：`FsOperation.read_file`、`openjiuwen.harness.prompts.sections.context._read_context_file`。

## 数据结构

- `_config_cache`：deep adapter 在 agent 模式为 **react 配置 dict**（非整份 config.yaml）；读取 `context_engine_config` 时**不要**再 `.get("react")`。
- `_context_assemble_mode`：跟踪 Assemble 是否按 agent 模式注册。

## 执行路径全景

### P1：实例创建

```
create_instance → apply_context_read_patch()
  → context_engine_config from _deep_agent_context_engine_config
  → rails 实例化含 overflow recovery（非 flash 裁剪后）
```

### P2：热重载 config

```
_apply_reload_config_snapshot / _update_agent_rails
  → 重新读 enabled → 注册或卸载 ContextProcessorRail
  → 重建 Assemble（disabled_tools + eager allowlist）
```

### P3：Progressive 工具变更

```
ProgressiveToolRail eager_tools 更新
  → _sync_context_assemble_tool_allowlist(assemble, progressive)
```

## 修改检查单

- 改 `context_engine_config.enabled` 默认值 → **同时**评估 interface_deep 与 `get_context_engine_enabled`，更新 N1 与 yaml 注释。
- 改 flash 白名单 → 确认 context_* rail 是否应保留；更新 [[S_01]] N5。
- 新增 adapter 模式 → 在本 spec 模式表增列并定义 overflow/assemble/processor 策略。
- 改 patch 挂载点 → `test_context_read_patch.py` + [[S_07]] 人设读路径。

## 与其它 spec 的关系

- [[S_01]]：模块边界
- [[S_03]]：Assemble/Processor 门控
- [[S_05]]：overflow rail 装配差异
- [[S_07]]：prompt rails 在 deep 装配列表中的位置（grep `_build_agent_rails` / rail_infos）
