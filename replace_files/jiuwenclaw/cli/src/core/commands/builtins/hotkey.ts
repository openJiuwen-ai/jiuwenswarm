import { APP_SCREEN_KEY_BINDINGS } from "../../../ui/keymap.js";
import { addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

export function createHotkeyCommand(): SlashCommand {
  return {
    name: "hotkey",
    description: "Show available keyboard shortcuts",
    usage: "/hotkey",
    example: "/hotkey",
    kind: CommandKind.BUILT_IN,
    action: (ctx) => {
      ctx.addItem(
        addInfo(ctx.sessionId, "Keyboard shortcuts", "k", {
          view: "list",
          title: "Hotkeys",
          items: [
            ...APP_SCREEN_KEY_BINDINGS.map((binding) => ({
              label: binding.label,
              description: binding.description,
            })),
            { label: "/help", description: "show slash commands" },
          ],
        }),
      );
    },
  };
}
