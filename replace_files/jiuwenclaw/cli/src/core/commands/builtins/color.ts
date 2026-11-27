import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import { getAccentColorOptions } from "../../../ui/theme.js";

const COLOR_OPTIONS = getAccentColorOptions();

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
      if (!COLOR_OPTIONS.includes(value as (typeof COLOR_OPTIONS)[number])) {
        ctx.addItem(
          addError(
            ctx.sessionId,
            `invalid color "${value}". available: ${COLOR_OPTIONS.join(", ")}`,
          ),
        );
        return;
      }
      ctx.setAccentColor(value as (typeof COLOR_OPTIONS)[number]);
      ctx.addItem(addInfo(ctx.sessionId, `Session accent color set to ${value}`, "c"));
    },
  };
}
