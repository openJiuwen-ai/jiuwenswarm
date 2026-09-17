import { flattenArrayPayload, formatValue, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createSessionsCommand(): SlashCommand {
  return {
    name: "sessions",
    description: "List sessions",
    usage: "/sessions",
    example: "/sessions",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      const payload = await ctx.request("session.list", {});
      const items = flattenArrayPayload(payload).map((item, index) => ({
        label: String(index + 1),
        value: typeof item === "string" ? item : formatValue(item),
      }));
      ctx.addItem(
        makeItem(
          ctx.sessionId,
          "info",
          items.length > 0 ? "Available sessions" : "No sessions returned",
          "s",
          {
            view: "list",
            title: "Sessions",
            items,
          },
        ),
      );
    },
  };
}
