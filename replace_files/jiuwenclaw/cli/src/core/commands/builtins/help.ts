import { CommandKind, type SlashCommand, type SlashCommandListProvider } from "../types.js";
import { makeItem } from "../helpers.js";

export function createHelpCommand(getCommands: SlashCommandListProvider): SlashCommand {
  return {
    name: "help",
    description: "Show available commands",
    usage: "/help",
    example: "/help",
    kind: CommandKind.BUILT_IN,
    action: (ctx) => {
      const lines = getCommands()
        .filter((command) => !command.hidden)
        .map((command) => ({
          label: `/${command.name}`,
          value: command.usage?.replace(/^\/[^\s]+/, "").trim() || undefined,
          description: command.description,
        }));
      ctx.addItem(
        makeItem(ctx.sessionId, "info", "Available commands", "?", {
          view: "help",
          title: "Slash Commands",
          items: lines,
        }),
      );
    },
  };
}
