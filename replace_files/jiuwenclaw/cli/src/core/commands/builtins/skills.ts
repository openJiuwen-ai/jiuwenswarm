import { flattenArrayPayload, formatValue, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

type MarketPlaceItem = {
  name: string;
  url: string;
  enabled: boolean;
  install_location?: string | null;
  last_updated?: string | null;
};

async function listMarketplaces(ctx: import("../types.js").CommandContext): Promise<void> {
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
    makeItem(
      ctx.sessionId,
      "info",
      items.length > 0 ? "Marketplace sources" : "No marketplace sources",
      "*",
      { view: "list", title: "Marketplaces", items },
    ),
  );
}

async function listSkills(ctx: import("../types.js").CommandContext): Promise<void> {
  const payload = await ctx.request("skills.list", {});
  const items = flattenArrayPayload(payload).map((item, index) => {
    if (item && typeof item === "object") {
      const obj = item as Record<string, unknown>;
      return {
        label: typeof obj.name === "string" ? obj.name : String(index + 1),
        value: typeof obj.path === "string" ? obj.path : undefined,
        description: typeof obj.description === "string" ? obj.description : undefined,
      };
    }
    return { label: String(index + 1), value: formatValue(item) };
  });
  ctx.addItem(
    makeItem(
      ctx.sessionId,
      "info",
      items.length > 0 ? "Installed skills" : "No skills returned",
      "*",
      { view: "list", title: "Skills", items },
    ),
  );
}

export function createSkillsCommand(): SlashCommand {
  return {
    name: "skills",
    description: "Manage skills (list, install, uninstall, marketplace)",
    usage: "/skills [list|install|uninstall|marketplace]",
    example: "/skills list",
    kind: CommandKind.BUILT_IN,
    action: async (ctx) => {
      await listSkills(ctx);
    },
    subCommands: [
      {
        name: "list",
        description: "List skills",
        usage: "/skills list",
        example: "/skills list",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          await listSkills(ctx);
        },
      },
      {
        name: "install",
        description: "Install a skill from marketplace",
        usage: "/skills install <spec>",
        example: "/skills install my-skill@marketplace",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const spec = args.trim();
          if (!spec) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills install <plugin@marketplace>"));
            return;
          }
          const payload = await ctx.request<{ success?: boolean; detail?: string }>(
            "skills.install",
            { spec, force: false },
            120_000,
          );
          if (payload.success) {
            ctx.addItem(makeItem(ctx.sessionId, "info", `Skill installed: ${spec}`));
          } else {
            ctx.addItem(
              makeItem(ctx.sessionId, "error", payload.detail || `Install failed: ${spec}`),
            );
          }
        },
      },
      {
        name: "uninstall",
        description: "Uninstall a skill",
        usage: "/skills uninstall <name>",
        example: "/skills uninstall my-skill",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const name = args.trim();
          if (!name) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills uninstall <name>"));
            return;
          }
          const payload = await ctx.request<{ success?: boolean; detail?: string }>(
            "skills.uninstall",
            { name },
            120_000,
          );
          if (payload.success) {
            ctx.addItem(makeItem(ctx.sessionId, "info", `Skill uninstalled: ${name}`));
          } else {
            ctx.addItem(
              makeItem(ctx.sessionId, "error", payload.detail || `Uninstall failed: ${name}`),
            );
          }
        },
      },
      {
        name: "marketplace",
        description: "Manage marketplace sources",
        usage: "/skills marketplace [list|add|remove|toggle]",
        example: "/skills marketplace list",
        kind: CommandKind.BUILT_IN,
        action: async (ctx) => {
          await listMarketplaces(ctx);
        },
        subCommands: [
          {
            name: "list",
            description: "List marketplace sources",
            usage: "/skills marketplace list",
            example: "/skills marketplace list",
            kind: CommandKind.BUILT_IN,
            takesArgs: false,
            action: async (ctx) => {
              await listMarketplaces(ctx);
            },
          },
          {
            name: "add",
            description: "Add a marketplace source",
            usage: "/skills marketplace add <name> <url>",
            example: "/skills marketplace add my-repo https://github.com/user/skills",
            kind: CommandKind.BUILT_IN,
            takesArgs: true,
            action: async (ctx, args) => {
              const parts = args.trim().split(/\s+/);
              const name = parts[0];
              const url = parts.slice(1).join(" ");
              if (!name || !url) {
                ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills marketplace add <name> <url>"));
                return;
              }
              const payload = await ctx.request<{ success?: boolean; detail?: string }>(
                "skills.marketplace.add",
                { name, url },
              );
              if (payload.success) {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Marketplace added: ${name}`));
                await listMarketplaces(ctx);
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || `Add failed: ${name}`));
              }
            },
          },
          {
            name: "remove",
            description: "Remove a marketplace source",
            usage: "/skills marketplace remove <name>",
            example: "/skills marketplace remove my-repo",
            kind: CommandKind.BUILT_IN,
            takesArgs: true,
            action: async (ctx, args) => {
              const name = args.trim();
              if (!name) {
                ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills marketplace remove <name>"));
                return;
              }
              const payload = await ctx.request<{ success?: boolean; detail?: string }>(
                "skills.marketplace.remove",
                { name, remove_cache: true },
              );
              if (payload.success) {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Marketplace removed: ${name}`));
                await listMarketplaces(ctx);
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || `Remove failed: ${name}`));
              }
            },
          },
          {
            name: "toggle",
            description: "Enable/disable a marketplace source",
            usage: "/skills marketplace toggle <name> <on|off>",
            example: "/skills marketplace toggle my-repo on",
            kind: CommandKind.BUILT_IN,
            takesArgs: true,
            action: async (ctx, args) => {
              const parts = args.trim().split(/\s+/);
              const name = parts[0];
              const enabledStr = parts[1]?.toLowerCase();
              if (!name || !enabledStr) {
                ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills marketplace toggle <name> <on|off>"));
                return;
              }
              const enabled = enabledStr === "on" || enabledStr === "true" || enabledStr === "1";
              const payload = await ctx.request<{ success?: boolean; detail?: string }>(
                "skills.marketplace.toggle",
                { name, enabled },
                120_000,
              );
              if (payload.success) {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Marketplace ${name}: ${enabled ? "enabled" : "disabled"}`));
                await listMarketplaces(ctx);
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || `Toggle failed: ${name}`));
              }
            },
          },
        ],
      },
    ],
  };
}
