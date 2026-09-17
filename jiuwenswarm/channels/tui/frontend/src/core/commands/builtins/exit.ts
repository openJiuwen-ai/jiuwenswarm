import { CommandKind, type SlashCommand } from "../types.js";

export function createExitCommand(): SlashCommand {
  return {
    name: "exit",
    altNames: ["quit"],
    description: "Exit the REPL",
    usage: "/exit",
    example: "/quit",
    kind: CommandKind.BUILT_IN,
    action: (ctx) => {
      ctx.exitApp();
    },
  };
}
