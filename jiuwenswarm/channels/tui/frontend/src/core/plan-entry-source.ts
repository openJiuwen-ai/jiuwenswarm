/**
 * `plan_entry_source` 字面量契约常量。
 *
 * 这是前后端共享的硬契约：后端
 * `jiuwenswarm.common.schema.chat_send.PLAN_ENTRY_SOURCES`（含
 * `PLAN_ENTRY_SOURCE_SLASH_COMMAND = "slash_command"` 与
 * `PLAN_ENTRY_SOURCE_PLAN_TOGGLE = "plan_toggle"`）与本文件的
 * `PLAN_ENTRY_SOURCE_SLASH_COMMAND` 必须保持同名字面量。TUI 端虽然只产
 * `slash_command`（`/plan` 命令），但完整集合常量与 Web
 * `jiuwenswarm/channels/web/frontend/src/features/planMode/planEntrySource.ts`
 * 对齐，便于跨端核对防重入闸门可识别的全部来源。
 *
 * 改动这些取值前先跑：
 *   - 后端 `tests/unit_tests/test_plan_entry_source_contract.py`
 *   - TUI `cd jiuwenswarm/channels/tui/frontend && npm test`
 *
 * 后端 `AgentWebSocketServer._is_explicit_plan_entry_request` 防重入闸门只认这些
 * 字面量；TUI `/plan` 命令通过 `app-state.ts` 把 `pendingPlanEntrySource`
 * 序列化进 `chat.send` 的 `plan_entry_source` 字段。两边字面量不一致会导致
 * 防重入闸门对 TUI 失效（用户下一条消息会被静默拖回 plan）。
 */

/** TUI 的 `/plan` 命令产出的 entry source。 */
export const PLAN_ENTRY_SOURCE_SLASH_COMMAND = "slash_command" as const;

/**
 * Web 用户手动打开 Plan 开关的那一条消息产出的 entry source。TUI 端不产出
 * 这个值（只产 `slash_command`），但作为跨端契约对齐的一部分在此声明，
 * 便于在调试或后续复用 Plan 入口路径时与后端字面量保持一致。
 */
export const PLAN_ENTRY_SOURCE_PLAN_TOGGLE = "plan_toggle" as const;

/**
 * TUI 端已知的合法 plan entry source 集合（与后端 `PLAN_ENTRY_SOURCES` 对齐）。
 *
 * 包含 `slash_command`（TUI 产）与 `plan_toggle`（Web 产）；TUI 实际只发送
 * `slash_command`，集合里同时列出 `plan_toggle` 是为了让前端能在调试或跨端
 * 互操作时识别所有合法来源（防重入闸门在后端接受两者）。
 */
export const PLAN_ENTRY_SOURCES: ReadonlySet<string> = new Set<string>([
  PLAN_ENTRY_SOURCE_SLASH_COMMAND,
  PLAN_ENTRY_SOURCE_PLAN_TOGGLE,
]);

/** `pendingPlanEntrySource` 字段的合法取值（TUI 端只产 `slash_command`）。 */
export type PlanEntrySource = typeof PLAN_ENTRY_SOURCE_SLASH_COMMAND;
