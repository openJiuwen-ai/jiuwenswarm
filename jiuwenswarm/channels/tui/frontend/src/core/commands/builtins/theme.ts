import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { ThemeName } from "../../../ui/theme.js";

const DISPLAY_OPTIONS: readonly ["dark", "light"] = ["dark", "light"];

export function createThemeCommand(): SlashCommand {
  return {
    name: "theme",
    description: "Change the theme",
    usage: "/theme [dark|light]",
    example: "/theme dark",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...DISPLAY_OPTIONS],
    action: (ctx, args) => {
      const value = args.trim().toLowerCase();
      if (!value) {
        ctx.addItem(
          addInfo(ctx.sessionId, `Current theme: ${ctx.themeName}`, "t", {
            view: "list",
            title: "Theme",
            items: DISPLAY_OPTIONS.map((option) => ({
              label:
                option === ctx.themeName ? `${option} (current)` : option,
            })),
          }),
        );
        return;
      }

      if (!DISPLAY_OPTIONS.includes(value as "dark" | "light")) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            `invalid theme "${value}". available: ${DISPLAY_OPTIONS.join(", ")}`,
          ),
        );
        return;
      }

      ctx.setThemeName(value as ThemeName);
      ctx.addItem(addInfo(ctx.sessionId, `Theme set to ${value}`, "t"));
    },
  };
}
