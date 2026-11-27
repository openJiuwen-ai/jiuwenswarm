import { addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createPlaceholderCommand(options: {
  name: string;
  description: string;
  usage: string;
  example: string;
  altNames?: string[];
  takesArgs?: boolean;
  completion?: SlashCommand["completion"];
  message: string | ((args: string) => string);
}): SlashCommand {
  return {
    name: options.name,
    altNames: options.altNames,
    description: options.description,
    usage: options.usage,
    example: options.example,
    kind: CommandKind.BUILT_IN,
    takesArgs: options.takesArgs,
    completion: options.completion,
    action: (ctx, args) => {
      const message =
        typeof options.message === "function" ? options.message(args) : options.message;
      ctx.addItem(addInfo(ctx.sessionId, message, "i"));
    },
  };
}
