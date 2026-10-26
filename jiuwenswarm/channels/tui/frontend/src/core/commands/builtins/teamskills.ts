import { flattenArrayPayload, makeItem, parseArgs } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

type TeamSkillsPublishParams = {
  path?: string;
  file?: string;
  skill_id?: string;
  version?: string;
  version_desc?: string;
  token?: string;
  system_token?: string;
  market_url?: string;
  force?: boolean;
};

type TeamSkillsDeleteParams = {
  skill_id?: string;
  version?: string;
  token?: string;
  system_token?: string;
  market_url?: string;
};

type TeamSkillsInitParams = {
  name?: string;
  path?: string;
  force?: boolean;
  skill_type?: "teamskills" | "skill";
};

type TeamSkillsValidateParams = {
  path?: string;
  skill_type?: "teamskills" | "skill";
};

type TeamSkillsPackParams = {
  path?: string;
  output?: string;
};

type TeamSkillsInfoParams = {
  asset_id?: string;
  version?: string;
  market_url?: string;
};

type TeamSkillsSearchParams = {
  q: string;
  page_size?: number;
  page?: number;
  skill_type?: string;
  author?: string;
  search_asset_id?: string;
  search_asset_type?: string;
  search_publisher_id?: string;
  order_by?: string;
  desc?: boolean;
  market_url?: string;
};

type TeamSkillsInstallParams = {
  asset_id?: string;
  version?: string;
  output?: string;
  force?: boolean;
  market_url?: string;
};

type TeamSkillsConfigParams = {
  market_url?: string;
  token?: string;
  system_token?: string;
  allowed_download_hosts?: string;
};

function parseFlagArgs(rawArgs: string): { positionals: string[]; flags: Record<string, string | boolean> } {
  const tokens = parseArgs(rawArgs);
  const positionals: string[] = [];
  const flags: Record<string, string | boolean> = {};
  const shortAlias: Record<string, string> = {
    f: "file",
    v: "version",
    o: "output",
  };

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (!token.startsWith("-")) {
      positionals.push(token);
      continue;
    }

    let key = "";
    if (token.startsWith("--")) {
      const withoutPrefix = token.slice(2);
      const eqIndex = withoutPrefix.indexOf("=");
      if (eqIndex > -1) {
        key = withoutPrefix.slice(0, eqIndex);
        flags[key] = withoutPrefix.slice(eqIndex + 1);
        continue;
      }
      key = withoutPrefix;
    } else {
      const shortKey = token.slice(1);
      key = shortAlias[shortKey] || shortKey;
    }
    const next = tokens[i + 1];
    if (!next || next.startsWith("-")) {
      flags[key] = true;
      continue;
    }

    flags[key] = next;
    i += 1;
  }

  return { positionals, flags };
}

function buildPublishParams(args: string): TeamSkillsPublishParams {
  const { positionals, flags } = parseFlagArgs(args);
  return {
    path: positionals[0],
    file: typeof flags.file === "string" ? flags.file : undefined,
    skill_id: typeof flags.id === "string" ? flags.id : undefined,
    version: typeof flags.version === "string" ? flags.version : undefined,
    version_desc: typeof flags["version-desc"] === "string" ? flags["version-desc"] : undefined,
    token: typeof flags.token === "string" ? flags.token : undefined,
    system_token: typeof flags["system-token"] === "string" ? flags["system-token"] : undefined,
    market_url: typeof flags["market-url"] === "string" ? flags["market-url"] : undefined,
    force: Boolean(flags.force),
  };
}

function buildDeleteParams(args: string): TeamSkillsDeleteParams {
  const { positionals, flags } = parseFlagArgs(args);
  return {
    skill_id: positionals[0],
    version: typeof flags.version === "string" ? flags.version : undefined,
    token: typeof flags.token === "string" ? flags.token : undefined,
    system_token: typeof flags["system-token"] === "string" ? flags["system-token"] : undefined,
    market_url: typeof flags["market-url"] === "string" ? flags["market-url"] : undefined,
  };
}

function buildInfoParams(args: string): TeamSkillsInfoParams {
  const { positionals, flags } = parseFlagArgs(args);
  return {
    asset_id: positionals[0],
    version: typeof flags.version === "string" ? flags.version : undefined,
    market_url: typeof flags["market-url"] === "string" ? flags["market-url"] : undefined,
  };
}

function buildInitParams(args: string): TeamSkillsInitParams {
  const { positionals, flags } = parseFlagArgs(args);
  const pluginTypeRaw = typeof flags.type === "string" ? flags.type.toLowerCase() : undefined;
  return {
    name: positionals[0],
    path: typeof flags.path === "string" ? flags.path : undefined,
    force: Boolean(flags.force),
    skill_type: pluginTypeRaw === "skill" || pluginTypeRaw === "teamskills" ? pluginTypeRaw : undefined,
  };
}

function buildValidateParams(args: string): TeamSkillsValidateParams {
  const { positionals, flags } = parseFlagArgs(args);
  const pluginTypeRaw = typeof flags.type === "string" ? flags.type.toLowerCase() : undefined;
  return {
    path: positionals[0],
    skill_type: pluginTypeRaw === "skill" || pluginTypeRaw === "teamskills" ? pluginTypeRaw : undefined,
  };
}

function buildPackParams(args: string): TeamSkillsPackParams {
  const { positionals, flags } = parseFlagArgs(args);
  return {
    path: positionals[0],
    output: typeof flags.output === "string" ? flags.output : undefined,
  };
}

function buildSearchParams(args: string, defaultQuery = ""): TeamSkillsSearchParams {
  const { positionals, flags } = parseFlagArgs(args);
  const pageSizeRaw =
    typeof flags["page-size"] === "string"
      ? Number(flags["page-size"])
      : typeof flags.limit === "string"
        ? Number(flags.limit)
        : undefined;
  const pageRaw = typeof flags.page === "string" ? Number(flags.page) : undefined;
  const descRaw = flags.desc;
  return {
    q: positionals[0] || defaultQuery,
    page_size: Number.isInteger(pageSizeRaw) ? pageSizeRaw : undefined,
    page: Number.isInteger(pageRaw) ? pageRaw : undefined,
    skill_type: typeof flags.type === "string" ? flags.type : undefined,
    author: typeof flags.author === "string" ? flags.author : undefined,
    search_asset_id: typeof flags["asset-id"] === "string" ? flags["asset-id"] : undefined,
    search_asset_type: typeof flags["asset-type"] === "string" ? flags["asset-type"] : undefined,
    search_publisher_id:
      typeof flags["publisher-id"] === "string" ? flags["publisher-id"] : undefined,
    order_by: typeof flags["order-by"] === "string" ? flags["order-by"] : undefined,
    desc:
      typeof descRaw === "string"
        ? ["1", "true", "yes", "on"].includes(descRaw.toLowerCase())
        : typeof descRaw === "boolean"
          ? descRaw
          : undefined,
    market_url: typeof flags["market-url"] === "string" ? flags["market-url"] : undefined,
  };
}

function buildInstallParams(args: string): TeamSkillsInstallParams {
  const { positionals, flags } = parseFlagArgs(args);
  return {
    asset_id: positionals[0],
    version: typeof flags.version === "string" ? flags.version : undefined,
    output: typeof flags.output === "string" ? flags.output : undefined,
    force: Boolean(flags.force),
    market_url: typeof flags["market-url"] === "string" ? flags["market-url"] : undefined,
  };
}

function buildConfigParams(args: string): TeamSkillsConfigParams {
  const { flags } = parseFlagArgs(args);
  return {
    market_url: typeof flags["market-url"] === "string" ? flags["market-url"] : undefined,
    token: typeof flags.token === "string" ? flags.token : undefined,
    system_token: typeof flags["system-token"] === "string" ? flags["system-token"] : undefined,
    allowed_download_hosts:
      typeof flags["allowed-download-hosts"] === "string" ? flags["allowed-download-hosts"] : undefined,
  };
}

function validateExactlyOneAuth(token?: string, systemToken?: string): string | null {
  const hasToken = Boolean(token && token.trim());
  const hasSystemToken = Boolean(systemToken && systemToken.trim());
  if (hasToken === hasSystemToken) {
    return "请且仅请提供一种鉴权方式：--token 或 --system-token";
  }
  return null;
}

function formatMaybe(value: unknown): string {
  if (value === null || value === undefined || value === "") return "unknown";
  return String(value);
}

function normalizeTeamSkillsErrorDetail(detail: unknown): string {
  if (detail === null || detail === undefined) return "请求失败";
  const raw = String(detail).trim();
  if (!raw) return "请求失败";
  let parsed: unknown = null;
  if ((raw.startsWith("{") && raw.endsWith("}")) || (raw.startsWith("[") && raw.endsWith("]"))) {
    try {
      parsed = JSON.parse(raw);
    } catch {
      parsed = null;
    }
  }
  if (!parsed || typeof parsed !== "object") {
    const plain = raw
      .replaceAll("插件", "技能")
      .replace(/\bplugin_id\b/g, "skill_id")
      .replace(/\bplugin\b/gi, "skill");
    return plain;
  }

  const obj = parsed as Record<string, unknown>;
  const detailObj =
    obj.detail && typeof obj.detail === "object" ? (obj.detail as Record<string, unknown>) : obj;
  const code = detailObj.code;
  const error = typeof detailObj.error === "string" ? detailObj.error : "";
  const rawMessage = typeof detailObj.message === "string" ? detailObj.message : "";
  const message = rawMessage
    .replaceAll("插件", "技能")
    .replace(/\bplugin_id\b/g, "skill_id")
    .replace(/\bplugin\b/gi, "skill");

  if (error === "invalid_plugin_config") {
    return `${message || "技能配置格式错误或缺失"}\n建议：检查并补齐 SKILL.md/frontmatter，重新执行 /teamskills validate 后再发布。`;
  }
  if (error === "plugin_not_found") {
    return `${message || "技能不存在"}\n建议：请使用 skill_id（不是名称）重试，可先用 /teamskills search --asset-id <skill_id> 或 /teamskills info <skill_id> --version <x.y.z> 校验。`;
  }
  if (error.includes("token") || /unauthorized|forbidden/i.test(message)) {
    return `${message || "鉴权失败"}\n建议：检查 /teamskills config 的 token/system-token，且二者只能配置一种。`;
  }
  if (error === "version_conflict") {
    return `${message || "版本冲突"}\n建议：如需覆盖请添加 --force；若不覆盖请升级 --version。`;
  }

  const parts: string[] = [];
  if (message) parts.push(message);
  if (error) parts.push(`error=${error}`);
  if (code !== undefined && code !== null && code !== "") parts.push(`code=${String(code)}`);
  return parts.length > 0 ? parts.join(" | ") : raw;
}

async function hydrateTeamSkillsAuthFromConfig(
  ctx: { request: <T>(method: string, params: Record<string, unknown>) => Promise<T> },
  params: { token?: string; system_token?: string },
): Promise<void> {
  if ((params.token && params.token.trim()) || (params.system_token && params.system_token.trim())) {
    return;
  }
  const config = await ctx.request<Record<string, unknown>>("config.get", {});
  const token = String(config.teamskills_user_token || "").trim();
  const systemToken = String(config.teamskills_system_token || "").trim();
  if (token) params.token = token;
  if (systemToken) params.system_token = systemToken;
}

async function hydrateTeamSkillsMarketUrlFromConfig(
  ctx: { request: <T>(method: string, params: Record<string, unknown>) => Promise<T> },
  params: { market_url?: string },
): Promise<void> {
  if (params.market_url && params.market_url.trim()) {
    return;
  }
  const config = await ctx.request<Record<string, unknown>>("config.get", {});
  const marketUrl = String(config.teamskills_market_url || "").trim();
  if (marketUrl) params.market_url = marketUrl;
}

export function createTeamSkillsCommand(): SlashCommand {
  return {
    name: "teamskills",
    description: "Manage TeamSkills Hub (init, validate, pack, info, search, list, install, uninstall, publish, delete)",
    usage: "/teamskills [init|validate|pack|info|search|list|install|uninstall|config|publish|delete]",
    example: "/teamskills publish ./my-skill --version 1.0.0 --token <TOKEN>",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    action: async (ctx) => {
      ctx.addItem(
        makeItem(
          ctx.sessionId,
          "info",
          "用法: /teamskills init|validate|pack|info|search|list|install|uninstall|config|publish|delete。",
        ),
      );
    },
    subCommands: [
      {
        name: "init",
        description: "Create a TeamSkills scaffold",
        usage: "/teamskills init <name> [--path <parent_dir>] [--type <teamskills|skill>] [--force]",
        example: "/teamskills init demo-skill --path . --type teamskills",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildInitParams(args);
          if (!params.name) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：<name>"));
            return;
          }
          if (!params.skill_type) {
            params.skill_type = "teamskills";
          }
          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            path?: string;
          }>("skills.teamskillshub.init", params, 120_000);
          if (!payload.success) {
            ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || "初始化失败"));
            return;
          }
          ctx.addItem(makeItem(ctx.sessionId, "info", `初始化成功: ${payload.path || params.name}`));
        },
      },
      {
        name: "validate",
        description: "Validate TeamSkills directory",
        usage: "/teamskills validate <path> [--type <teamskills|skill>]",
        example: "/teamskills validate ./demo-skill --type teamskills",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildValidateParams(args);
          if (!params.path) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：<path>"));
            return;
          }
          if (!params.skill_type) {
            params.skill_type = "teamskills";
          }
          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            errors?: string[];
            name?: string;
            warnings?: string[];
          }>("skills.teamskillshub.validate", params, 120_000);
          if (!payload.success) {
            const issues = (payload.errors || []).filter((item) => typeof item === "string" && item.trim());
            const issueText = issues.length > 0 ? `\n- ${issues.join("\n- ")}` : "";
            const message = `${payload.detail || "校验失败"}${issueText}`;
            ctx.addItem(makeItem(ctx.sessionId, "error", message));
            return;
          }
          const warnings = payload.warnings || [];
          const content =
            warnings.length > 0
              ? `校验通过: ${payload.name || "unknown"}（${warnings.join("；")}）`
              : `校验通过: ${payload.name || "unknown"}`;
          ctx.addItem(makeItem(ctx.sessionId, "info", content));
        },
      },
      {
        name: "pack",
        description: "Pack TeamSkills into zip",
        usage: "/teamskills pack <path> [--output <dir>]",
        example: "/teamskills pack ./demo-skill --output out",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildPackParams(args);
          if (!params.path) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：<path>"));
            return;
          }
          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            path?: string;
          }>("skills.teamskillshub.pack", params, 120_000);
          if (!payload.success) {
            ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || "打包失败"));
            return;
          }
          ctx.addItem(makeItem(ctx.sessionId, "info", `打包成功: ${payload.path || "unknown"}`));
        },
      },
      {
        name: "info",
        description: "Show TeamSkills Hub asset version details",
        usage: "/teamskills info <asset_id> --version <x.y.z> [--market-url <url>]",
        example: "/teamskills info sk-123 --version 1.0.0",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildInfoParams(args);
          if (!params.asset_id) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：<asset_id>"));
            return;
          }
          if (!params.version) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：--version <x.y.z>"));
            return;
          }
          await hydrateTeamSkillsMarketUrlFromConfig(ctx, params);
          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            asset_id?: string;
            version?: string;
            data?: Record<string, unknown>;
          }>("skills.teamskillshub.info", params);
          if (!payload.success) {
            ctx.addItem(
              makeItem(
                ctx.sessionId,
                "error",
                normalizeTeamSkillsErrorDetail(payload.detail || "获取 TeamSkills Hub 详情失败"),
              ),
            );
            return;
          }
          const data = payload.data || {};
          const lines = [
            `name=${formatMaybe(data.name)}`,
            `asset_id=${formatMaybe(payload.asset_id || params.asset_id)}`,
            `version=${formatMaybe(payload.version || params.version)}`,
            `display_name=${formatMaybe(data.display_name)}`,
            `checksum_sha256=${formatMaybe(data.checksum_sha256)}`,
            `download_url=${formatMaybe(data.download_url)}`,
            `size=${formatMaybe(data.file_size || data.size)}`,
            `updated_at=${formatMaybe(data.update_time || data.updated_at)}`,
          ];
          ctx.addItem(
            makeItem(ctx.sessionId, "info", `详情获取成功:\n${lines.map((line) => `- ${line}`).join("\n")}`),
          );
        },
      },
      {
        name: "search",
        description: "Search skills on TeamSkills Hub",
        usage:
          "/teamskills search <query> [--type <skill|teamskills>] [--author <name>] [--asset-id <id>] [--asset-type <type>] [--publisher-id <id>] [--page <n>] [--page-size <n>] [--order-by <field>] [--desc <bool>] [--market-url <url>]",
        example: "/teamskills search agent --type skill --page-size 20 --order-by install_count --desc true",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildSearchParams(args);
          await hydrateTeamSkillsMarketUrlFromConfig(ctx, params);
          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            detail_key?: string;
            skills?: Array<{
              asset_id?: string;
              name?: string;
              display_name?: string;
              summary?: string;
              version?: string;
            }>;
            count?: number;
          }>("skills.teamskillshub.search", params, 120_000);
          if (!payload.success) {
            ctx.addItem(
              makeItem(
                ctx.sessionId,
                "error",
                normalizeTeamSkillsErrorDetail(payload.detail || payload.detail_key || "搜索失败"),
              ),
            );
            return;
          }
          const skills = payload.skills || [];
          const items = skills.map((item) => ({
            label: `${item.name || "unknown"} | ${item.asset_id || "unknown"} | ${item.version ? `v${item.version}` : "unknown"}`,
          }));
          ctx.addItem(
            makeItem(
              ctx.sessionId,
              "info",
              `搜索完成，结果 ${payload.count ?? items.length} 条`,
              "*",
              {
                view: "list",
                title: `搜索完成，结果 ${payload.count ?? items.length} 条（格式：name | skill_id | version）`,
                items,
              },
            ),
          );
        },
      },
      {
        name: "list",
        description: "List installed skills with type",
        usage: "/teamskills list",
        example: "/teamskills list",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          const payload = await ctx.request("skills.list", {});
          const items = flattenArrayPayload(payload)
            .filter((item) => item && typeof item === "object")
            .map((item) => item as Record<string, unknown>)
            .map((item) => {
              const type = String(item.kind || "").trim() === "team-skill" ? "teamskills" : "skill";
              const path =
                typeof item.path === "string" && item.path.trim() ? item.path.trim() : "unknown";
              const description =
                typeof item.description === "string" && item.description.trim()
                  ? item.description.trim()
                  : "unknown";
              return {
                label: `[${type}] ${String(item.name || "unknown")}`,
                description: `Path: ${path}\nDescription: ${description}`,
              };
            });
          ctx.addItem(
            makeItem(
              ctx.sessionId,
              "info",
              items.length > 0 ? `已安装技能: ${items.length} 个` : "未找到已安装技能",
              "*",
              {
                view: "list",
                title: "Installed Skills（格式：[type] name）",
                items,
              },
            ),
          );
        },
      },
      {
        name: "install",
        description: "Install a skill from TeamSkills Hub",
        usage:
          "/teamskills install <asset_id> [--version <x.y.z>] [--output <dir>] [--force] [--market-url <url>]",
        example: "/teamskills install sk-123 --version 1.0.0 --output ./installed --force",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildInstallParams(args);
          if (!params.asset_id) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：<asset_id>"));
            return;
          }
          await hydrateTeamSkillsMarketUrlFromConfig(ctx, params);
          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            detail_key?: string;
            skill?: { name?: string; asset_id?: string; path?: string };
          }>("skills.teamskillshub.install", params, 180_000);
          if (!payload.success) {
            ctx.addItem(
              makeItem(
                ctx.sessionId,
                "error",
                normalizeTeamSkillsErrorDetail(payload.detail || payload.detail_key || "安装失败"),
              ),
            );
            return;
          }
          const displayName = payload.skill?.name || params.asset_id;
          const installPath = payload.skill?.path;
          const content = installPath
            ? `安装成功: ${displayName}\n安装位置: ${installPath}`
            : `安装成功: ${displayName}`;
          ctx.addItem(makeItem(ctx.sessionId, "info", content));
        },
      },
      {
        name: "uninstall",
        description: "Uninstall an installed TeamSkills skill",
        usage: "/teamskills uninstall <name>",
        example: "/teamskills uninstall teamskill-creator",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const name = args.trim();
          if (!name) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：<name>"));
            return;
          }
          const payload = await ctx.request<{ success?: boolean; detail?: string }>(
            "skills.uninstall",
            { name },
            120_000,
          );
          if (!payload.success) {
            ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || "卸载失败"));
            return;
          }
          ctx.addItem(makeItem(ctx.sessionId, "info", `卸载成功: ${name}`));
        },
      },
      {
        name: "config",
        description: "Configure TeamSkills Hub URL and tokens",
        usage:
          "/teamskills config [--market-url <url>] [--token <user_token>] [--system-token <system_token>] [--allowed-download-hosts <h1,h2,...>]",
        example:
          "/teamskills config --market-url https://teamskills.openjiuwen.com --token <TOKEN> --allowed-download-hosts 127.0.0.1,localhost",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildConfigParams(args);
          const updates: Record<string, string> = {};
          if (params.market_url) updates.teamskills_market_url = params.market_url;
          if (params.token) updates.teamskills_user_token = params.token;
          if (params.system_token) updates.teamskills_system_token = params.system_token;
          if (params.allowed_download_hosts) {
            updates.teamskills_allowed_download_hosts = params.allowed_download_hosts;
          }

          if (Object.keys(updates).length > 0) {
            const setPayload = await ctx.request<{
              updated?: string[];
              applied_without_restart?: boolean;
            }>("config.set", updates);
            const updatedKeys = setPayload.updated || [];
            const restartHint = setPayload.applied_without_restart ? "已即时生效" : "需重启后生效";
            ctx.addItem(
              makeItem(
                ctx.sessionId,
                "info",
                `TeamSkills 配置已更新: ${updatedKeys.length > 0 ? updatedKeys.join(", ") : "none"}（${restartHint}）`,
              ),
            );
          }

          const getPayload = await ctx.request<Record<string, unknown>>("config.get", {});
          const lines = [
            `market_url=${formatMaybe(getPayload.teamskills_market_url)}`,
            `token=${getPayload.teamskills_user_token ? "***" : "unknown"}`,
            `system_token=${getPayload.teamskills_system_token ? "***" : "unknown"}`,
            `allowed_download_hosts=${formatMaybe(getPayload.teamskills_allowed_download_hosts)}`,
          ];
          ctx.addItem(
            makeItem(
              ctx.sessionId,
              "info",
              `TeamSkills 当前配置:\n${lines.map((line) => `- ${line}`).join("\n")}`,
            ),
          );
        },
      },
      {
        name: "publish",
        description: "Publish a skill to TeamSkills Hub",
        usage:
          "/teamskills publish <path> [--file <zip>] --version <x.y.z> [--id <skill_id>] [--token <t>|--system-token <t>] [--market-url <url>] [--force] [--version-desc <text>]",
        example:
          "/teamskills publish ./demo-skill --version 1.0.0 --token <TOKEN> --market-url https://teamskills.openjiuwen.com",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildPublishParams(args);
          if (!params.version) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：--version <x.y.z>"));
            return;
          }

          await hydrateTeamSkillsMarketUrlFromConfig(ctx, params);
          await hydrateTeamSkillsAuthFromConfig(ctx, params);

          const authError = validateExactlyOneAuth(params.token, params.system_token);
          if (authError) {
            ctx.addItem(makeItem(ctx.sessionId, "error", authError));
            return;
          }

          if (!params.path && !params.file) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "请提供 <path> 或 --file <zip>"));
            return;
          }

          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            detail_key?: string;
            skill_id?: string;
            name?: string;
            version?: string;
          }>("skills.teamskillshub.publish", params, 180_000);

          if (!payload.success) {
            ctx.addItem(
              makeItem(
                ctx.sessionId,
                "error",
                normalizeTeamSkillsErrorDetail(payload.detail || payload.detail_key || "发布失败"),
              ),
            );
            return;
          }

          const displaySkillId = payload.skill_id || params.skill_id || "unknown";
          const displayName = payload.name || "unknown";
          const displayVersion = payload.version || params.version || "unknown";
          ctx.addItem(
            makeItem(
              ctx.sessionId,
              "info",
              `发布成功: skill_id=${displaySkillId}, name=${displayName}, version=${displayVersion}`,
            ),
          );
        },
      },
      {
        name: "delete",
        description: "Delete a skill/version from TeamSkills Hub",
        usage:
          "/teamskills delete <skill_id> [--version <x.y.z|all>] [--token <t>|--system-token <t>] [--market-url <url>]",
        example:
          "/teamskills delete sk-123 --version 1.0.0 --token <TOKEN> --market-url https://teamskills.openjiuwen.com",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const params = buildDeleteParams(args);
          if (!params.skill_id) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "缺少参数：<skill_id>"));
            return;
          }

          await hydrateTeamSkillsMarketUrlFromConfig(ctx, params);
          await hydrateTeamSkillsAuthFromConfig(ctx, params);

          const authError = validateExactlyOneAuth(params.token, params.system_token);
          if (authError) {
            ctx.addItem(makeItem(ctx.sessionId, "error", authError));
            return;
          }

          const payload = await ctx.request<{
            success?: boolean;
            detail?: string;
            detail_key?: string;
            skill_id?: string;
            version?: string;
          }>("skills.teamskillshub.delete", params, 120_000);

          if (!payload.success) {
            ctx.addItem(
              makeItem(
                ctx.sessionId,
                "error",
                normalizeTeamSkillsErrorDetail(payload.detail || payload.detail_key || "删除失败"),
              ),
            );
            return;
          }

          const deletedSkillId = payload.skill_id || params.skill_id;
          const deletedVersion = payload.version || params.version || "all";
          ctx.addItem(
            makeItem(ctx.sessionId, "info", `删除成功: skill_id=${deletedSkillId}, version=${deletedVersion}`),
          );
        },
      },
    ],
  };
}
