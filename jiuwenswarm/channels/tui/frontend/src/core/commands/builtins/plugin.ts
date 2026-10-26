import { addError, addInfo, flattenArrayPayload, parseArgs } from "../helpers.js";
import { CommandKind, type SlashCommand, type CommandContext } from "../types.js";

interface PluginEntry {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit: string;
  skills: string[];
  enabled: boolean;
}

interface PluginListPayload {
  plugins: PluginEntry[];
}

interface MarketPlaceItem {
  name: string;
  url: string;
  enabled: boolean;
  install_location?: string | null;
  last_updated?: string | null;
}

async function listPlugins(ctx: CommandContext): Promise<void> {
  try {
    const payload = await ctx.request("plugins.list", {}) as PluginListPayload;
    const plugins = payload.plugins ?? [];

    if (plugins.length === 0) {
      ctx.addItem(addInfo(ctx.sessionId, "No plugins installed", "plugin"));
      return;
    }

    const enabled = plugins.filter((p) => p.enabled);
    const disabled = plugins.filter((p) => !p.enabled);

    if (enabled.length > 0) {
      const items = enabled.map((p) => ({
        label: p.plugin_name,
        value: `${p.spec}  v${p.version || "?"}  skills: ${p.skills.join(", ") || "-"}`,
      }));
      ctx.addItem(
        addInfo(ctx.sessionId, `Enabled (${enabled.length})`, "plugin", {
          view: "list",
          title: "Enabled Plugins",
          items,
        }),
      );
    }

    if (disabled.length > 0) {
      const items = disabled.map((p) => ({
        label: p.plugin_name,
        value: `${p.spec}  v${p.version || "?"}  (use /plugin enable to re-enable)`,
      }));
      ctx.addItem(
        addInfo(ctx.sessionId, `Disabled (${disabled.length})`, "plugin", {
          view: "list",
          title: "Disabled Plugins",
          items,
        }),
      );
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    ctx.addItem(addError(ctx.sessionId, `Failed to list plugins: ${message}`));
  }
}

async function installPlugin(ctx: CommandContext, spec: string): Promise<void> {
  if (!spec) {
    ctx.addItem(addError(ctx.sessionId, "Usage: /plugin install <name@marketplace> | <path_or_url>"));
    return;
  }

  const isLocalPath = /^([A-Za-z]:[\\/]|\/|\.\/|\.\.\/)/.test(spec);
  const isUrl = /^https?:\/\/.+/i.test(spec);

  if (isLocalPath || isUrl) {
    ctx.addItem(addInfo(ctx.sessionId, `Importing plugin from: ${spec}`));
    const payload = await ctx.request<{ success?: boolean; detail?: string }>(
      "skills.import_local",
      { path: spec, force: false },
      120_000,
    );
    if (payload.success) {
      ctx.addItem(addInfo(ctx.sessionId, `Plugin imported: ${spec}`));
    } else {
      ctx.addItem(addError(ctx.sessionId, payload.detail || `Import failed: ${spec}`));
    }
    return;
  }

  ctx.addItem(addInfo(ctx.sessionId, `Installing plugin: ${spec}`));
  const payload = await ctx.request<{ success?: boolean; detail?: string }>(
    "plugins.install",
    { spec, force: false },
    120_000,
  );
  if (payload.success) {
    ctx.addItem(addInfo(ctx.sessionId, `Plugin installed: ${spec}`));
    await listPlugins(ctx);
  } else {
    ctx.addItem(addError(ctx.sessionId, payload.detail || `Install failed: ${spec}`));
  }
}

async function uninstallPlugin(ctx: CommandContext, name: string): Promise<void> {
  if (!name) {
    ctx.addItem(addError(ctx.sessionId, "Usage: /plugin uninstall <name>"));
    return;
  }
  const payload = await ctx.request<{ success?: boolean; detail?: string }>(
    "plugins.uninstall",
    { name },
    120_000,
  );
  if (payload.success) {
    ctx.addItem(addInfo(ctx.sessionId, `Plugin uninstalled: ${name}`));
  } else {
    ctx.addItem(addError(ctx.sessionId, payload.detail || `Uninstall failed: ${name}`));
  }
}

async function enablePlugin(ctx: CommandContext, name: string): Promise<void> {
  if (!name) {
    ctx.addItem(addError(ctx.sessionId, "Usage: /plugin enable <name>"));
    return;
  }
  const payload = await ctx.request<{ success?: boolean; detail?: string }>(
    "plugins.enable",
    { name },
  );
  if (payload.success) {
    ctx.addItem(addInfo(ctx.sessionId, payload.detail || `Plugin enabled: ${name}`));
  } else {
    ctx.addItem(addError(ctx.sessionId, payload.detail || `Enable failed: ${name}`));
  }
}

async function disablePlugin(ctx: CommandContext, name: string): Promise<void> {
  if (!name) {
    ctx.addItem(addError(ctx.sessionId, "Usage: /plugin disable <name>"));
    return;
  }
  const payload = await ctx.request<{ success?: boolean; detail?: string }>(
    "plugins.disable",
    { name },
  );
  if (payload.success) {
    ctx.addItem(addInfo(ctx.sessionId, payload.detail || `Plugin disabled: ${name}`));
  } else {
    ctx.addItem(addError(ctx.sessionId, payload.detail || `Disable failed: ${name}`));
  }
}

async function listMarketplaces(ctx: CommandContext): Promise<void> {
  const payload = await ctx.request<{ marketplaces?: MarketPlaceItem[] }>(
    "skills.marketplace.list",
    {},
  );
  const items = (payload.marketplaces || []).map((m) => ({
    label: m.name,
    value: m.url,
    description: `${m.enabled ? "enabled" : "disabled"} | ${m.last_updated || "never updated"}`,
  }));
  ctx.addItem(
    addInfo(
      ctx.sessionId,
      items.length > 0 ? "Marketplace sources" : "No marketplace sources",
      "marketplace",
      { view: "list", title: "Marketplaces", items },
    ),
  );
}

export function createPluginCommand(): SlashCommand {
  return {
    name: "plugin",
    altNames: ["plugins", "marketplace"],
    description: "Manage plugins (list, install, uninstall, enable, disable, marketplace)",
    usage: "/plugin [list|install|uninstall|enable|disable|marketplace]",
    example:
      "/plugin list\n" +
      "/plugin install my-plugin@clawhub\n" +
      "/plugin uninstall my-plugin\n" +
      "/plugin enable my-plugin\n" +
      "/plugin disable my-plugin\n" +
      "/plugin marketplace list",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: true,
    action: async (ctx: CommandContext, args: string) => {
      const raw = args.trim();
      const parts = parseArgs(raw);

      if (parts.length === 0 || parts[0] === "list") {
        await listPlugins(ctx);
        return;
      }

      const sub = parts[0];
      const rest = parts.slice(1).join(" ");

      switch (sub) {
        case "install":
          await installPlugin(ctx, rest);
          break;
        case "uninstall":
          await uninstallPlugin(ctx, rest);
          break;
        case "enable":
          await enablePlugin(ctx, rest);
          break;
        case "disable":
          await disablePlugin(ctx, rest);
          break;
        case "marketplace":
          if (parts[1] === "list" || !parts[1]) {
            await listMarketplaces(ctx);
          } else if (parts[1] === "add") {
            const mpName = parts[2];
            const mpUrl = parts.slice(3).join(" ");
            if (!mpName || !mpUrl) {
              ctx.addItem(addError(ctx.sessionId, "Usage: /plugin marketplace add <name> <url>"));
              return;
            }
            const mpPayload = await ctx.request<{ success?: boolean; detail?: string }>(
              "skills.marketplace.add",
              { name: mpName, url: mpUrl },
            );
            if (mpPayload.success) {
              ctx.addItem(addInfo(ctx.sessionId, `Marketplace added: ${mpName}`));
              await listMarketplaces(ctx);
            } else {
              ctx.addItem(addError(ctx.sessionId, mpPayload.detail || `Add failed: ${mpName}`));
            }
          } else if (parts[1] === "remove") {
            const mpName = parts[2];
            if (!mpName) {
              ctx.addItem(addError(ctx.sessionId, "Usage: /plugin marketplace remove <name>"));
              return;
            }
            const mpPayload = await ctx.request<{ success?: boolean; detail?: string }>(
              "skills.marketplace.remove",
              { name: mpName, remove_cache: true },
            );
            if (mpPayload.success) {
              ctx.addItem(addInfo(ctx.sessionId, `Marketplace removed: ${mpName}`));
              await listMarketplaces(ctx);
            } else {
              ctx.addItem(addError(ctx.sessionId, mpPayload.detail || `Remove failed: ${mpName}`));
            }
          } else if (parts[1] === "toggle") {
            const mpName = parts[2];
            const onOff = parts[3];
            if (!mpName || !onOff) {
              ctx.addItem(addError(ctx.sessionId, "Usage: /plugin marketplace toggle <name> on|off"));
              return;
            }
            const enabled = onOff.toLowerCase() === "on" || onOff.toLowerCase() === "true";
            const mpPayload = await ctx.request<{ success?: boolean; detail?: string }>(
              "skills.marketplace.toggle",
              { name: mpName, enabled },
              120_000,
            );
            if (mpPayload.success) {
              ctx.addItem(addInfo(ctx.sessionId, `Marketplace ${mpName}: ${enabled ? "enabled" : "disabled"}`));
              await listMarketplaces(ctx);
            } else {
              ctx.addItem(addError(ctx.sessionId, mpPayload.detail || `Toggle failed: ${mpName}`));
            }
          } else {
            ctx.addItem(addError(ctx.sessionId, `Unknown marketplace sub-command: "${parts[1]}". Use: list, add, remove, toggle`));
          }
          break;
        default:
          ctx.addItem(
            addError(
              ctx.sessionId,
              `Unknown sub-command: "${sub}". Use: list, install, uninstall, enable, disable, marketplace`,
            ),
          );
      }
    },
  };
}