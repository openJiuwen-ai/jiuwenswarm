import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import { getThemeOptions } from "../../../ui/theme.js";

const THEME_OPTIONS = getThemeOptions();

export function createThemeCommand(): SlashCommand {
  return {
    name: "theme",
    description: "Change the theme",
    usage: "/theme [system|dark|light]",
    example: "/theme dark",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...THEME_OPTIONS],
    action: (ctx, args) => {
      const value = args.trim().toLowerCase();
      if (!value) {
        ctx.addItem(
          addInfo(ctx.sessionId, `Current theme: ${ctx.themeName}`, "t", {
            view: "list",
            title: "Theme",
            items: THEME_OPTIONS.map((option) => ({
              label: option,
              description: option === ctx.themeName ? "current" : undefined,
            })),
          }),
        );
        return;
      }

      if (!THEME_OPTIONS.includes(value as (typeof THEME_OPTIONS)[number])) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            `invalid theme "${value}". available: ${THEME_OPTIONS.join(", ")}`,
          ),
        );
        return;
      }

      ctx.setThemeName(value as (typeof THEME_OPTIONS)[number]);
      ctx.addItem(addInfo(ctx.sessionId, `Theme set to ${value}`, "t"));
    },
  };
}
