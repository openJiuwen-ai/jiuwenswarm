/**
 * `plan_entry_source` 字面量契约常量。
 *
 * 这是前后端共享的硬契约：后端
 * `jiuwenswarm.common.schema.chat_send.PLAN_ENTRY_SOURCES`（含
 * `PLAN_ENTRY_SOURCE_PLAN_TOGGLE = "plan_toggle"` 与
 * `PLAN_ENTRY_SOURCE_SLASH_COMMAND = "slash_command"`）与本文件常量必须保持
 * 同名字面量。Web 端虽然只产 `plan_toggle`（开关手动打开），但完整集合常量
 * 与 TUI `jiuwenswarm/channels/tui/frontend/src/core/plan-entry-source.ts` 对齐，
 * 便于跨端核对防重入闸门可识别的全部来源。
 *
 * 改动这些取值前先跑：
 *   - 后端 `tests/unit_tests/test_plan_entry_source_contract.py`
 *   - Web 前端 `cd jiuwenswarm/channels/web/frontend && npm run test:wire-mode`
 *
 * 后端 `AgentWebSocketServer._is_explicit_plan_entry_request` 防重入闸门只认这些
 * 字面量；Web 用户手动打开 Plan 开关后的第一条 Plan 消息会通过
 * `useWebSocket.ts` 的 `resolvePlanEntryPayload` 把 `plan_entry_source` 字段
 * 序列化成 `plan_toggle`。两边字面量不一致会导致防重入闸门对 Web 失效
 * （开关不复位时下一条消息会被静默拖回 plan）。
 */

/** Web 用户手动打开 Plan 开关的那一条消息产出的 entry source。 */
export const PLAN_ENTRY_SOURCE_PLAN_TOGGLE = 'plan_toggle' as const;

/**
 * TUI 的 `/plan` 命令产出的 entry source。Web 端不产出这个值（只产
 * `plan_toggle`），但作为跨端契约对齐的一部分在此声明，便于在调试或后续
 * 复用 Plan 入口路径时与后端字面量保持一致。
 */
export const PLAN_ENTRY_SOURCE_SLASH_COMMAND = 'slash_command' as const;

/**
 * Web 端已知的合法 plan entry source 集合（与后端 `PLAN_ENTRY_SOURCES` 对齐）。
 *
 * 包含 `plan_toggle`（Web 产）与 `slash_command`（TUI 产）；Web 实际只发送
 * `plan_toggle`，集合里同时列出 `slash_command` 是为了让前端能在调试或跨端
 * 互操作时识别所有合法来源（防重入闸门在后端接受两者）。
 */
export const PLAN_ENTRY_SOURCES: ReadonlySet<string> = new Set<string>([
  PLAN_ENTRY_SOURCE_PLAN_TOGGLE,
  PLAN_ENTRY_SOURCE_SLASH_COMMAND,
]);
