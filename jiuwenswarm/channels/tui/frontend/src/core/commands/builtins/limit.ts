import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

function formatCost(value: number): string {
  return value.toFixed(4);
}

function showLimit(ctx: import("../types.js").CommandContext): void {
  const summary = ctx.getUsageSummary();
  const limit = summary.cost_limit ?? null;
  const items = [
    { label: "scope", value: "session" },
    { label: "cost_limit", value: limit === null ? "infinite" : formatCost(limit) },
  ];
  if (summary.cost_available && typeof summary.total_cost === "number") {
    items.push({ label: "cost_source", value: summary.cost_source ?? "provider_reported" });
    if (summary.currency) {
      items.push({ label: "currency", value: summary.currency });
    }
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
    description: "Show or set the current session cost limit",
    usage: "/limit [cost <amount>|session cost <amount>|clear|session clear]",
    example: "/limit cost 2.50",
    kind: CommandKind.BUILT_IN,
    action: async (ctx, rawArgs = "") => {
      const args = rawArgs.trim().split(/\s+/).filter(Boolean);
      if (args.length === 0) {
        showLimit(ctx);
        return;
      }
      if (args[0] === "task") {
        ctx.addItem(addError(ctx.sessionId, "Task-scoped cost limits are not supported yet; use session scope."));
        return;
      }
      const scope = "session";
      const offset = args[0] === "session" ? 1 : 0;
      const command = args[offset];
      if (command === "clear") {
        await syncBackendLimit(ctx, `/${["limit", scope, "clear"].filter(Boolean).join(" ")}`);
        ctx.addItem(addInfo(ctx.sessionId, "Session cost limit cleared (infinite)", "l"));
        return;
      }
      if (command === "cost") {
        const value = parseCost(args[offset + 1]);
        if (value === null) {
          ctx.addItem(addError(ctx.sessionId, "Usage: /limit [cost <amount>|session cost <amount>|clear|session clear]"));
          return;
        }
        await syncBackendLimit(ctx, `/${["limit", scope, "cost", value.toString()].filter(Boolean).join(" ")}`);
        ctx.addItem(addInfo(ctx.sessionId, `Requested session cost limit: ${formatCost(value)}`, "l"));
        return;
      }
      ctx.addItem(addError(ctx.sessionId, "Usage: /limit [cost <amount>|session cost <amount>|clear|session clear]"));
    },
  };
}
