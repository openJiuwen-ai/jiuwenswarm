import { addInfo, addError } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

function showUsage(ctx: import("../types.js").CommandContext): void {
  const summary = ctx.getUsageSummary();
  const fmt = (n: number) => n.toLocaleString("en-US");

  const items = [
    { label: "input_tokens", value: fmt(summary.total_input_tokens) },
    { label: "output_tokens", value: fmt(summary.total_output_tokens) },
    { label: "total_tokens", value: fmt(summary.total_tokens) },
  ];

  if (summary.byModel.length > 0) {
    for (const entry of summary.byModel) {
      items.push(
        { label: `model: ${entry.model}`, value: `${fmt(entry.total_tokens)} tokens` },
        { label: `  input`, value: fmt(entry.input_tokens) },
        { label: `  output`, value: fmt(entry.output_tokens) },
      );
    }
  }

  ctx.addItem(
    addInfo(ctx.sessionId, "Session usage", "u", {
      view: "kv",
      title: "Usage",
      items,
    }),
  );
}

export function createUsageCommand(): SlashCommand {
  return {
    name: "usage",
    description: "Show session token usage (input / output / total)",
    usage: "/usage",
    example: "/usage",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    action: async (ctx) => {
      if (ctx.enterStatusView) {
        ctx.enterStatusView("usage");
        return;
      }
      try {
        showUsage(ctx);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `usage failed: ${message}`));
      }
    },
  };
}