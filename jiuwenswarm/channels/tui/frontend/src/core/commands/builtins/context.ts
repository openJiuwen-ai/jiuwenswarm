import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export interface ContextUsagePayload {
  context_window_limit: number;
  total_tokens: number;
  system_prompt_tokens: number;
  messages_tokens: number;
  tools_tokens: number;
  occupancy_rate: number;
  message_count: number;
  context_occupancy: unknown;
}

function formatTokenCount(n: number | undefined): string {
  const value = n ?? 0;
  if (value < 1000) {
    return value.toLocaleString("en-US");
  }
  return `${Number((value / 1000).toFixed(1))}k`;
}

function toLocale(n: number | undefined): string {
  return (n ?? 0).toLocaleString("en-US");
}

function renderBar(percent: number, width: number): string {
  const filled = Math.round((percent / 100) * width);
  const clamped = Math.min(filled, width);
  const empty = width - clamped;
  return "[" + "=".repeat(clamped) + (empty > 0 ? ">" : "") + " ".repeat(Math.max(0, empty - 1)) + "]";
}

function showContextUsage(
  ctx: import("../types.js").CommandContext,
  payload: ContextUsagePayload,
): void {
  const pct = payload.occupancy_rate;
  const bar = renderBar(pct, 30);
  const heading = pct >= 90 ? `Context window ${pct}% full — consider /compact` : `Context window ${pct}% full`;

  ctx.addItem(
    addInfo(ctx.sessionId, heading, "c", {
      view: "kv",
      title: `Context — Overview  ${bar}  ${pct}%`,
      items: [
        {
          label: "context_window",
          value: `${formatTokenCount(payload.total_tokens)} / ${formatTokenCount(payload.context_window_limit)} tokens`,
        },
        { label: "occupancy", value: `${pct}%` },
        { label: "messages", value: toLocale(payload.message_count) },
      ],
    }),
  );

  ctx.addItem(
    addInfo(ctx.sessionId, "Token breakdown by component", "t", {
      view: "kv",
      title: "Context — Token Breakdown",
      items: [
        { label: "system_prompt", value: formatTokenCount(payload.system_prompt_tokens) },
        { label: "messages", value: formatTokenCount(payload.messages_tokens) },
        { label: "tools", value: formatTokenCount(payload.tools_tokens) },
        { label: "total", value: formatTokenCount(payload.total_tokens) },
      ],
    }),
  );

  if (payload.context_occupancy) {
    const occ = payload.context_occupancy as Record<string, unknown>;
    const occItems = Object.entries(occ).map(([k, v]) => ({
      label: k,
      value: typeof v === "number" ? formatTokenCount(v) : String(v),
    }));
    ctx.addItem(
      addInfo(ctx.sessionId, "DeepAgent context occupancy details", "d", {
        view: "kv",
        title: "Context — Occupancy",
        items: occItems.length > 0 ? occItems : [{ label: "—", value: "No data" }],
      }),
    );
  }
}

export function createContextCommand(): SlashCommand {
  return {
    name: "context",
    description: "Show context window usage and token breakdown",
    usage: "/context",
    example: "/context",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: false,
    action: async (ctx) => {
      try {
        const payload = await ctx.request<ContextUsagePayload>("command.context", {
          mode: ctx.mode,
        });
        showContextUsage(ctx, payload);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `context failed: ${message}`));
      }
    },
  };
}