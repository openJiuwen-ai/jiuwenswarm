# relay 注入内容与 flash 自有段去重

| 项 | 值 |
| --- | --- |
| 类型 | bugfix |
| 日期 | 2026-11-25 |
| 关联规约 | [S_05 agent skill rails](../../../../server/runtime/skill/docs/specs/S_05_agent-skill-rails.md) |

relay 产品包（`RelayClawAgentService.ts` 拼装、经 `system_prompt` 下发）与 flash 自有段存在两处重复，根因是去重只做单向、且产品补丁块持续新增：

1. `[记忆纠正规则]` 第 6 条「删除=回收站即为合规的软删除」vs 120 删除规范（120 含大文件同卷回退例外，更精确）；
2. `[记忆纠正规则]` 仍用 `read_memory` / `edit_memory` / `write_memory`，与 600 已改写为 `memory(mode=…)` 的统一入口并存。

修复（只动 flash 侧，common 与 deep 行为不变）：

- **删第 6 条**：仅当本轮已生成等价的 120 删除规范（`deletion_protocol` 段含「回收站 + 已处理」约束）时删除 relay 简版；safety 域关闭或拆分回退时 120 不存在，relay 第 6 条是唯一来源，保留。按「行首编号 + 关键短语」定位，编号漂移也能命中；其余 1–5 条保留（flash 无对应物，整块删除会有损）。
- **工具名改写**：映射表下沉到 `prompt/content/memory_names.py`，`FlashMemoryRail` 与 `FlashRelayMergeRail` 共用同一常量；只改写 `[记忆纠正规则]` 块内的旧工具名，不碰人格段等同段内其它文本；锚点缺失即原样返回。
- **块边界锚定**：`[文件输出约束]` / `[记忆纠正规则]` 一律按「独占一行的分区标题」定位（`relay.py` 的 `_find_section_header`），不认正文里的同名提及。`request_system_prompt` 还携带调用方人格、交互提示与 Add-in 上下文；人格正文若提前写到「如需解释 [记忆纠正规则]」，裸子串 `find` 会抢走块起点——人格里的 `read_memory.md` 被误改成 `memory(mode=read).md`，真块工具名漏改、第 6 条漏删。锚定后两类误判都不再发生。
- **守卫回退**：`[文件输出约束]` 锚点缺失/布局漂移时整体回退，原内容重加，记忆块两项改写一并跳过。

不处理（避免把非重复项当成重复）：

- **`## SOUL.md` / `## AGENT.md` 不做剥离**：relay 的 `system_prompt` 由 `SystemPromptBuilder.buildStaticIdentity` 拼装（`你是…` / `角色：` / `性格：` / `渠道口径：` + 产品补丁块），不携带身份文件内容；SOUL.md / AGENT.md / USER.md / IDENTITY.md 由引擎 `ContextAssembleRail`（priority 85）从 workspace 读取后注入 `context.soul` / `context.agent` / `context.user` / `context.identity`。两侧不存在同名副本，按 `## SOUL.md` 锚点剥离反而会误伤 relay 后续新增的分区。
- **`[Personality Override]`** 维持原样——flash 尚未实现人格段动态填充，且 target_agent 场景下它可能是唯一人格来源；需产品侧根因修复（agent `.md` 物化 / `soul` 被消费）后再评估。

回归：`test_flash_relay_merge_rail.py` 覆盖第 6 条删除（含编号漂移）、工具名改写与空操作、[文件输出约束] 原有守卫，以及「人格正文提前提及同名标签」的对抗场景（人格文件名保留 / 真块工具名改写 / 第 6 条删除 / 人格正文不被截断）；离线组装与数据面用例保持全绿。