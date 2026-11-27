import { addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createCancelCommand(): SlashCommand {
  return {
    name: "cancel",
    description: "Cancel the active request",
    usage: "/cancel",
    example: "/cancel",
    isSafeConcurrent: true,
    kind: CommandKind.BUILT_IN,
    action: (ctx) => {
      ctx.sendEventOnly("chat.interrupt", { intent: "cancel" });
      ctx.addItem(addInfo(ctx.sessionId, "Task interrupted", "i"));
    },
  };
}
