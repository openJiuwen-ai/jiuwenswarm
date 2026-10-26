import { addError, addInfo } from "../helpers.js";
import { CommandKind, type SlashCommand, type CommandContext } from "../types.js";

interface ReloadPluginsPayload {
  success: boolean;
  plugins_count: number;
  disabled_count: number;
  skills_count: number;
  detail: string;
}

export function createReloadPluginsCommand(): SlashCommand {
  return {
    name: "reload-plugins",
    description: "Activate pending plugin changes in the current session",
    usage: "/reload-plugins",
    example: "/reload-plugins",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: false,
    isSafeConcurrent: true,
    action: async (ctx: CommandContext) => {
      try {
        const payload = await ctx.request("plugins.reload", {}) as ReloadPluginsPayload;
        ctx.addItem(
          addInfo(ctx.sessionId, payload.detail || "Plugins reloaded", "plugin", {
            view: "kv",
            title: "Plugin Reload",
            items: [
              { label: "plugins", value: String(payload.plugins_count) },
              { label: "skills", value: String(payload.skills_count) },
              { label: "disabled", value: String(payload.disabled_count) },
            ],
          }),
        );
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `Failed to reload plugins: ${message}`));
      }
    },
  };
}