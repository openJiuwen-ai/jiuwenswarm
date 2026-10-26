import { addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createSwarmFlowsCommand(): SlashCommand {
  return {
    name: "swarmflows",
    altNames: ["swarmworkflows"],
    description: "Show swarm workflow runs for the current session",
    usage: "/swarmflows",
    example: "/swarmflows",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      ctx.addItem(
        addInfo(
          ctx.sessionId,
          "Swarm flows view is handled by the TUI. If this message appears, reopen the command from the interactive TUI.",
          "w",
        ),
      );
    },
  };
}
