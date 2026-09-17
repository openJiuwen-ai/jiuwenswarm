import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

const EXPAND_SCOPES = ["last", "all"] as const;

export function createExpandCommand(): SlashCommand {
  return {
    name: "expand",
    description: "Expand tool groups",
    usage: "/expand <last|all>",
    example: "/expand all",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...EXPAND_SCOPES],
    action: async (ctx, args) => {
      const scope = args.trim();
      if (!EXPAND_SCOPES.includes(scope as (typeof EXPAND_SCOPES)[number])) {
        ctx.addItem(makeItem(ctx.sessionId, "error", "usage: /expand <last|all>"));
        return;
      }
      ctx.expandToolGroups(scope as (typeof EXPAND_SCOPES)[number]);
      ctx.addItem(
        makeItem(
          ctx.sessionId,
          "info",
          `Expanded ${scope === "all" ? "all tool groups" : "latest tool group"}`,
          "+",
        ),
      );
    },
  };
}
