import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import { getAccentColorOptions, type AccentColorName } from "../../../ui/theme.js";

const COLOR_OPTIONS = getAccentColorOptions();
const RESET_ALIASES = ["default", "reset", "none", "gray", "grey"] as const;

function normalizeColorArg(value: string): AccentColorName | null {
  if (RESET_ALIASES.includes(value as (typeof RESET_ALIASES)[number])) {
    return "default";
  }
  if (COLOR_OPTIONS.includes(value as AccentColorName)) {
    return value as AccentColorName;
  }
  return null;
}

export function createColorCommand(): SlashCommand {
  return {
    name: "color",
    description: "Set the prompt bar color for this session",
    usage: "/color <color|default>",
    example: "/color blue",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...COLOR_OPTIONS],
    action: (ctx, args) => {
      const value = args.trim().toLowerCase();
      if (!value) {
        ctx.addItem(
          addInfo(ctx.sessionId, `Current color: ${ctx.accentColor}`, "c", {
            view: "list",
            title: "Accent Color",
            items: COLOR_OPTIONS.map((option) => ({
              label: option,
              description: option === ctx.accentColor ? "current" : undefined,
            })),
          }),
        );
        return;
      }
      const normalizedColor = normalizeColorArg(value);
      if (!normalizedColor) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            `invalid color "${value}". available: ${COLOR_OPTIONS.join(", ")}`,
          ),
        );
        return;
      }
      ctx.setAccentColor(normalizedColor);
      void ctx.request("session.color_set", {
        session_id: ctx.sessionId,
        color: normalizedColor,
      }).catch(() => {});
      ctx.addItem(addInfo(ctx.sessionId, `Session accent color set to ${normalizedColor}`, "c"));
    },
  };
}
