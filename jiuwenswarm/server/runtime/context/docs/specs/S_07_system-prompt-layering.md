# 系统提示词分层

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / system-prompt-layering |
| 关联模块 | agent-core harness prompts/sections + swarm Runtime/Response rails + prompt/ |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

- **谁往 system prompt 里写什么**：工作区静态人设文件、core context sections、swarm 动态 rails、压缩/召回相关提示段。
- **RuntimePromptRail** / **ResponsePromptRail** 注入的 section 名与职责。
- 与 **ContextAssembleRail** 产出（messages + tools 段）的边界：Assemble 管**窗口消息与工具清单段**，rails 管 **system_prompt_builder sections**。

### 本规约不管什么

- 用户单轮 `query` 与 multimodal 附件内容 → chat pipeline。
- Skill 协议 prompt rails → `server/runtime/skill/docs/specs/S_05_agent-skill-rails.md`。
- A2UI 专用 instruction 全集 → a2ui 包（ResponsePromptRail 仅条件注入 A2UI section）。

## 不变量

- **N1（人设文件归 core section）**：工作区 **AGENT.md / SOUL.md** 等由 agent-core `harness.prompts.sections.context` 读取并注入；swarm 通过 **context_read_patch** 优化读路径（[[S_06]] N3），**不** duplicate 正文到 yaml。
- **N2（Runtime 动态、每轮刷新）**：RuntimePromptRail 在 **`before_model_call`** 读 `runtime_state.yaml`、时钟、shell 环境、trusted dirs、browser 策略等；priority **5**。Good：时间/模式变更不依赖工具调用。Bad：把 runtime 状态写进静态 AGENT.md。
- **N3（Response 格式归 ResponsePromptRail）**：回复格式、thinking discipline、final visible reply 规则由 **ResponsePromptRail** 注入 section 名 `response` / `LocalSectionName.*`；A2UI section 按 channel/query 条件追加。
- **N4（Attachments）**：RuntimePromptRail 使用 **PromptAttachmentManager**（`attachment` sections，实现见 runtime_prompt_rail.py 约 87–130 行一带的 attachment 逻辑）；与 core 附件 kind 枚举协作。
- **N5（压缩召回用法）**：若模型需要知道 `[[HANDLE]]` / `[[COMPRESSION_RECALL]]` 用法，提示段由 **agent-core processor / context sections** 或产品 prompt 段承担——新增时须登记本 spec 与 [[S_04]]，避免 duplicate 冲突。

## 接口契约

### Core 工作区人设（协作）

- 读取入口：`_read_context_file`（被 patch 包装）
- 典型 file_key：AGENT、SOUL 等（以 core `sections.context` 为准）

### RuntimePromptRail（swarm）

| 生命周期 | 行为 |
|---|---|
| `init` | 绑定 `system_prompt_builder`、`prompt_attachment_manager` |
| `before_model_call` | 注入/更新 time、runtime、language、env、browser、tui、trusted_dirs、request_system_prompt 等 |
| `uninit` | remove_section 清理 |

构造参数：`language`、`channel`、`timezone_offset`、`agent_id`、`service_id`；per-request setter：`set_language`、`set_channel`、`set_trusted_dirs`、`set_runtime_paths`、`set_model_name`、`set_mode`、`set_session_id`、`set_force_english`（code 脚手架英文）、`set_request_system_prompt`。

辅助模块：

- `shell_environment.build_shell_environment_prompt`
- `prompt_builder` / `user_prompt_builder` / `browser_task_prompt`

### ResponsePromptRail（swarm）

| 属性 | 值 |
|---|---|
| `priority` | 5 |
| `_RESPONSE_SECTION_NAMES` | response、THINKING_DISCIPLINE、FINAL_VISIBLE_REPLY |
| A2UI | 条件 section `LocalSectionName.A2UI` |

`before_invoke`：写入 channel / skip_a2ui / a2ui_browser_workflow 到 ctx.extra。

## 数据结构

- **runtime_state.yaml**：按 session 隔离路径（RuntimePromptRail `set_session_id`）；失败时 fallback 到 rail 上缓存的 model/mode。
- **system_prompt_builder**：持有命名 section 有序列表；rails 仅 add/remove_section，不直接改最终字符串缓存（遵循 core builder 契约）。

## 执行路径全景

### P1：静态人设

```
ContextAssemble / core prompt assembly
  → read AGENT/SOUL/... via _read_context_file (patch: only_read=True)
  → merge into system sections
```

### P2：动态 runtime + response

```
before_model_call:
  RuntimePromptRail → time + runtime paths + env + attachments
  ResponsePromptRail → response + thinking + final reply (+ optional A2UI)
  → 与 Assemble 组装的 messages 一并送模
```

### P3：压缩相关提示

```
Processor 替换/卸载消息 + optional COMPRESSION_RECALL/HANDLE 标记
  → 窗口内保留索引段；召回工具/标记由 [[S_04]] 负责
```

## 修改检查单

- 新增 system section 名 → 登记 Runtime 或 Response rail 的 init/uninit 清理列表，避免泄漏到下一 session。
- 改 AGENT/SOUL 读逻辑 → core + context_read_patch + [[S_06]]；不在 swarm 复制第二套读文件。
- 改 attachment kind 或 section 87–130 行为 → 跑相关 harness 单测 + 本 spec N4。
- 与 skill / retrieval prompt rail priority 冲突 → 对照 skill 模块 S_05 priority 表，避免覆盖 tools/skills 段。

## 与其它 spec 的关系

- [[S_03]]：Assemble 与 prompt rails 分工
- [[S_04]]：HANDLE / COMPRESSION_RECALL 语义
- [[S_06]]：context_read_patch
- [[S_01]]：边界（skill prompt rails 不归本模块）
