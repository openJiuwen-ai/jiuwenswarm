import { addInfo } from "../helpers.js";
import type { ClientMode } from "../../modes.js";
import { CommandKind, type SlashCommand } from "../types.js";

const PLAN_TO_NORMAL: Record<ClientMode, ClientMode> = {
  "agent.work.normal": "agent.work.plan",
  "agent.work.plan": "agent.work.normal",
  "agent.code.normal": "agent.code.plan",
  "agent.code.plan": "agent.code.normal",
  "team.work.normal": "team.work.plan",
  "team.work.plan": "team.work.normal",
  "team.code.normal": "team.code.plan",
  "team.code.plan": "team.code.normal",
};

/** Toggle between plan and normal variants while preserving role+environment. */
export function resolvePlanTarget(mode: ClientMode): ClientMode {
  // Non-plan mode → flip to the corresponding plan variant.
  if (!mode.endsWith(".plan")) return PLAN_TO_NORMAL[mode] ?? "agent.work.plan";
  // Already plan → stay plan (no-op when called from /plan <desc>).
  return mode;
}

/** Resolve the normal counterpart when /plan is used to exit plan mode. */
export function resolveNormalTarget(mode: ClientMode): ClientMode | undefined {
  if (!mode.endsWith(".plan")) return undefined;
  return PLAN_TO_NORMAL[mode];
}

export function createPlanCommand(): SlashCommand {
  return {
    name: "plan",
    description: "Switch to plan mode, or send a planning request",
    usage: "/plan [open|<description>]",
    example: "/plan outline the migration steps",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const value = args.trim();
      const isPlan = ctx.mode.endsWith(".plan");
      // 仅无参时才对称退出（plan -> normal）。带参数（/plan <desc>）停留在当前
      // plan 态发送规划请求，与旧行为一致；否则 plan 态下会误退出并以 normal 发送。
      const exitPlan = isPlan && !value;
      let target: ClientMode;
      if (exitPlan) {
        const normalTarget = resolveNormalTarget(ctx.mode);
        if (!normalTarget) return;
        target = normalTarget;
      } else {
        // 已在 plan 且带参时 resolvePlanTarget 保持原样（no-op），即停留 plan 发送。
        target = resolvePlanTarget(ctx.mode);
      }
      if (ctx.mode !== target) {
        ctx.setMode(target);
      }
      // 仅进入/停留在 plan 态时才标记 plan_entry_source=slash_command，
      // 后端防重入闸门据此放行本次显式进入。退出 plan（exitPlan）路径
      // 目标是 normal 模式，不需要也不应携带 entry source——否则退出后
      // 紧接着 sendMessage 会误把 pendingPlanEntrySource 留在 normal 模式上
      // （isPlanClientMode 守门虽不序列化，但语义混乱且未来易踩坑）。
      if (!exitPlan) {
        ctx.markPlanEntryFromSlashCommand?.();
      }

      if (!value) {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            exitPlan ? "Plan mode disabled" : "Plan mode enabled",
            "p",
          ),
        );
        return;
      }

      if (value === "open") {
        ctx.addItem(
          addInfo(
            ctx.sessionId,
            "Plan mode is active. Type your planning request directly or run /plan <description>.",
            "p",
          ),
        );
        return;
      }

      const requestId = ctx.sendMessage(value, undefined, target);
      if (!requestId) {
        ctx.addItem(
          addInfo(ctx.sessionId, "offline: waiting for reconnect before sending plan request", "p"),
        );
      }
    },
  };
}
