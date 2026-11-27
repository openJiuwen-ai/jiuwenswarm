import { makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

const COLLAPSE_SCOPES = ["last", "all"] as const;

export function createCollapseCommand(): SlashCommand {
  return {
    name: "collapse",
    description: "Collapse tool groups",
    usage: "/collapse <last|all>",
    example: "/collapse last",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...COLLAPSE_SCOPES],
    action: async (ctx, args) => {
      const scope = args.trim();
      if (!COLLAPSE_SCOPES.includes(scope as (typeof COLLAPSE_SCOPES)[number])) {
        ctx.addItem(makeItem(ctx.sessionId, "error", "usage: /collapse <last|all>"));
        return;
      }
      ctx.collapseToolGroups(scope as (typeof COLLAPSE_SCOPES)[number]);
      ctx.addItem(
        makeItem(
          ctx.sessionId,
          "info",
          `Collapsed ${scope === "all" ? "all tool groups" : "latest tool group"}`,
          "-",
        ),
      );
    },
  };
}
