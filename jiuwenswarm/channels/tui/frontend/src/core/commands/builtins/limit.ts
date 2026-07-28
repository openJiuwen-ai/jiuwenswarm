import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

function formatCost(value: number): string {
  return `$${value.toFixed(4)}`;
}

function showLimit(ctx: import("../types.js").CommandContext): void {
  const summary = ctx.getUsageSummary();
  const limit = ctx.getSessionCostLimit?.() ?? null;
  const items = [
    { label: "scope", value: "session" },
    { label: "cost_limit", value: limit === null ? "infinite" : formatCost(limit) },
  ];
  if (summary.cost_available && typeof summary.total_cost === "number") {
    items.push({ label: "current_cost", value: formatCost(summary.total_cost) });
    if (limit !== null) {
      items.push({
        label: "remaining",
        value: formatCost(Math.max(0, limit - summary.total_cost)),
      });
    }
  } else {
    items.push({ label: "current_cost", value: "unavailable (provider did not return cost)" });
  }
  ctx.addItem(
    addInfo(ctx.sessionId, "Cost limit", "l", {
      view: "kv",
      title: "Limit",
      items,
    }),
  );
}

function parseCost(raw: string | undefined): number | null {
  if (!raw) return null;
  const normalized = raw.trim().replace(/^\$/, "");
  if (!normalized) return null;
  const value = Number(normalized);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

async function syncBackendLimit(
  ctx: import("../types.js").CommandContext,
  commandText: string,
): Promise<void> {
  try {
    await ctx.request(
      "chat.send",
      { query: commandText, mode: ctx.mode, log_as_user: false },
      30_000,
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    ctx.addItem(addError(ctx.sessionId, `backend cost limit sync failed: ${message}`));
  }
}

export function createLimitCommand(): SlashCommand {
  return {
    name: "limit",
    description: "Show or set current task/session cost limit",
    usage: "/limit [cost <amount>|session cost <amount>|task cost <amount>|clear]",
    example: "/limit cost 2.50",
    kind: CommandKind.BUILT_IN,
    action: async (ctx, rawArgs = "") => {
      const args = rawArgs.trim().split(/\s+/).filter(Boolean);
      if (args.length === 0) {
        showLimit(ctx);
        return;
      }
      const scope = args[0] === "session" ? "session" : args[0] === "task" ? "task" : "session";
      const offset = args[0] === "session" || args[0] === "task" ? 1 : 0;
      const scopeLabel = scope === "task" ? "Current task/session" : "Session";
      const command = args[offset];
      if (command === "clear") {
        ctx.setSessionCostLimit?.(null);
        await syncBackendLimit(ctx, `/${["limit", scope, "clear"].filter(Boolean).join(" ")}`);
        ctx.addItem(addInfo(ctx.sessionId, `${scopeLabel} cost limit cleared (infinite)`, "l"));
        return;
      }
      if (command === "cost") {
        const value = parseCost(args[offset + 1]);
        if (value === null) {
          ctx.addItem(addError(ctx.sessionId, "Usage: /limit [cost <amount>|session cost <amount>|task cost <amount>|clear]"));
          return;
        }
        ctx.setSessionCostLimit?.(value);
        const summary = ctx.getUsageSummary();
        const suffix = summary.cost_available
          ? ""
          : " It will not be enforced until provider cost metadata is available.";
        await syncBackendLimit(ctx, `/${["limit", scope, "cost", value.toString()].filter(Boolean).join(" ")}`);
        ctx.addItem(addInfo(ctx.sessionId, `${scopeLabel} cost limit set to ${formatCost(value)}.${suffix}`, "l"));
        return;
      }
      ctx.addItem(addError(ctx.sessionId, "Usage: /limit [cost <amount>|session cost <amount>|task cost <amount>|clear]"));
    },
  };
}
