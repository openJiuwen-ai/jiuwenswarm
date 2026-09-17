import { flattenArrayPayload, makeItem } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

type SkillNetItem = {
  skill_name: string;
  skill_description: string;
  author: string;
  stars: number;
  skill_url: string;
  category: string;
};

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

/** Poll SkillNet install_status until done, failed, or timeout.
 *  Returns the final status payload. */
async function pollSkillNetInstall(
  ctx: import("../types.js").CommandContext,
  installId: string,
  maxWaitMs: number = 15 * 60 * 1000,
  pollMs: number = 800,
): Promise<{ success?: boolean; status?: string; detail?: string; detail_key?: string; skill?: { name?: string; source?: string } }> {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, pollMs));
    const st = await ctx.request<{ success?: boolean; status?: string; detail?: string; detail_key?: string; skill?: { name?: string; source?: string } }>(
      "skills.skillnet.install_status",
      { install_id: installId },
      10_000,
    );
    if (st.status === "done") return st;
    if (st.status === "failed") return st;
    if (!st.success && st.status !== "pending") return st;
    // still pending — keep polling
  }
  return { success: false, detail: `SkillNet install timed out after ${Math.round(maxWaitMs / 1000)}s` };
}

/** Detect "skill already installed" from a SkillNet install result (sync response or polled final status).
 *  Backend signals this via detail_key "skills.skillNet.errors.skillAlreadyInstalled"
 *  or a Chinese detail containing 已安装/已存在. */
function isSkillNetAlreadyInstalled(result: { detail?: string; detail_key?: string } | undefined): boolean {
  if (!result) return false;
  if (result.detail_key === "skills.skillNet.errors.skillAlreadyInstalled") return true;
  return !!(result.detail?.includes("已存在") || result.detail?.includes("已安装"));
}

/** After a SkillNet install reports "already installed" (the async backend detects this
 *  during polling, not in the initial sync response), prompt the user to overwrite or cancel.
 *  Returns true if handled (overwrite attempted or cancelled), false if not an "already installed" case. */
async function promptSkillNetOverwrite(
  ctx: import("../types.js").CommandContext,
  url: string,
  result: { detail?: string; detail_key?: string } | undefined,
): Promise<boolean> {
  if (!isSkillNetAlreadyInstalled(result)) return false;
  const answers = await ctx.askQuestions([
    {
      header: "SkillNet",
      question: `Skill already exists. Do you want to force overwrite it from SkillNet?`,
      options: [
        { label: "Yes, overwrite", description: `Re-install from SkillNet, replacing the existing version` },
        { label: "No, cancel", description: "Keep the existing skill unchanged" },
      ],
    },
  ]);
  const selected = answers[0]?.selected_options?.[0];
  if (selected !== "Yes, overwrite") {
    ctx.addItem(makeItem(ctx.sessionId, "info", `Installation cancelled. Skill remains unchanged.`));
    return true;
  }
  ctx.addItem(makeItem(ctx.sessionId, "info", `Force re-installing from SkillNet: ${url}`));
  const forcePayload = await ctx.request<{ success?: boolean; detail?: string; pending?: boolean; install_id?: string }>(
    "skills.skillnet.install",
    { url, force: true },
    120_000,
  );
  if (forcePayload.success && forcePayload.pending && forcePayload.install_id) {
    ctx.addItem(makeItem(ctx.sessionId, "info", `Downloading... (install_id: ${forcePayload.install_id.slice(0, 8)})`));
    const finalSt = await pollSkillNetInstall(ctx, forcePayload.install_id);
    if (finalSt.success && finalSt.status === "done") {
      ctx.addItem(makeItem(ctx.sessionId, "info", `Skill re-installed from SkillNet: ${finalSt.skill?.name || url}`));
    } else {
      ctx.addItem(makeItem(ctx.sessionId, "error", finalSt.detail || `SkillNet force install failed: ${url}`));
    }
  } else if (forcePayload.success && !forcePayload.pending) {
    ctx.addItem(makeItem(ctx.sessionId, "info", `Skill re-installed from SkillNet: ${url}`));
  } else {
    ctx.addItem(makeItem(ctx.sessionId, "error", forcePayload.detail || `SkillNet force install failed: ${url}`));
  }
  return true;
}

async function listSkills(ctx: import("../types.js").CommandContext): Promise<void> {
  const payload = await ctx.request("skills.list", {});
  const allSkills = flattenArrayPayload(payload);

  const installed: { label: string; value?: string; description: string }[] = [];
  const available: { label: string; value?: string; description: string }[] = [];

  for (const item of allSkills) {
    if (item && typeof item === "object") {
      const obj = item as Record<string, unknown>;
      const name = typeof obj.name === "string" ? obj.name : "?";
      const sourceTag = obj.is_builtin_source ? "[builtin]" : (obj.source === "local" ? "[local]" : `[${obj.source || "project"}]`);
      const desc = typeof obj.description === "string" ? obj.description : "";
      const entry = {
        label: name,
        value: typeof obj.path === "string" ? obj.path : undefined,
        description: `${sourceTag} ${desc}`,
      };
      if (obj.installed === true) {
        installed.push(entry);
      } else {
        available.push(entry);
      }
    }
  }

  // Group 1: Installed skills
  ctx.addItem(
    makeItem(
      ctx.sessionId,
      "info",
      installed.length > 0 ? `Installed (${installed.length})` : "No installed skills",
      "*",
      { view: "list", title: "Installed Skills", items: installed },
    ),
  );

  // Group 2: Available skills (not yet installed)
  if (available.length > 0) {
    ctx.addItem(
      makeItem(
        ctx.sessionId,
        "info",
        `Available to install (${available.length})`,
        "*",
        { view: "list", title: "Available Skills", items: available },
      ),
    );
  }
}

export function createSkillsCommand(): SlashCommand {
  return {
    name: "skills",
    description: "Manage skills (list, install, uninstall, marketplace, skillnet, use)",
    usage: "/skills [list|install|uninstall|marketplace|skillnet|use]",
    example: "/skills install my-skill  |  /skills install code-review@clawhub  |  /skills skillnet search code",
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
        description: "Install a skill (builtin name, slug@clawhub, name@skillnet, plugin@marketplace, or local path/URL)",
        usage: "/skills install <skill> | <slug@clawhub> | <name@skillnet> | <skill@marketplace> | <path_or_url>",
        example: "/skills install my-skill  |  /skills install code-review@clawhub  |  /skills install code-review@skillnet",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const spec = args.trim();
          if (!spec) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills install <skill> | <slug@clawhub> | <name@skillnet> | <skill@marketplace> | <path_or_url>"));
            return;
          }

          // Detect local path / URL — delegate to import_local
          const isLocalPath = /^([A-Za-z]:[\\/]|\/|\.\/|\.\.\/)/.test(spec);
          const isUrl = /^https?:\/\/.+/i.test(spec);
          if (isLocalPath || isUrl) {
            ctx.addItem(makeItem(ctx.sessionId, "info", `Importing skill from: ${spec}`));
            const payload = await ctx.request<{ success?: boolean; detail?: string; skill?: { name?: string } }>(
              "skills.import_local",
              { path: spec, force: false },
              120_000,
            );
            if (payload.success) {
              const skillName = payload.skill?.name || spec;
              ctx.addItem(makeItem(ctx.sessionId, "info", `Skill imported: ${skillName}`));
            } else if (payload.detail?.includes("已存在") || payload.detail?.includes("已安装")) {
              // Skill already installed — ask user if they want to force overwrite
              const existingName = payload.detail.replace(/^skill\s+/, "").replace(/[\s已存在安装]+.*$/, "") || spec;
              const answers = await ctx.askQuestions([
                {
                  header: "Import",
                  question: `Skill "${existingName}" is already installed. Do you want to force overwrite it?`,
                  options: [
                    { label: "Yes, overwrite", description: `Re-import from "${spec}", replacing the existing version` },
                    { label: "No, cancel", description: "Keep the existing skill unchanged" },
                  ],
                },
              ]);
              const selected = answers[0]?.selected_options?.[0];
              if (selected === "Yes, overwrite") {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Force re-importing from: ${spec}`));
                const forcePayload = await ctx.request<{ success?: boolean; detail?: string; skill?: { name?: string } }>(
                  "skills.import_local",
                  { path: spec, force: true },
                  120_000,
                );
                if (forcePayload.success) {
                  const skillName = forcePayload.skill?.name || spec;
                  ctx.addItem(makeItem(ctx.sessionId, "info", `Skill re-imported: ${skillName}`));
                } else {
                  ctx.addItem(makeItem(ctx.sessionId, "error", forcePayload.detail || `Import failed: ${spec}`));
                }
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Import cancelled. Skill remains unchanged.`));
              }
            } else {
              ctx.addItem(
                makeItem(ctx.sessionId, "error", payload.detail || `Import failed: ${spec}`),
              );
            }
            return;
          }

          // ClawHub install flow: "ownerHandle/slug@clawhub" or "slug@clawhub"
          // ClawHub identifiers are alphanumeric slugs like "code-review", "daily-report" etc.
          if (spec.includes("@clawhub") || (spec.includes("@") && spec.endsWith("@clawhub"))) {
            const clawhubPart = spec.replace(/@clawhub$/i, "");
            let slug = clawhubPart;
            let ownerHandle: string | undefined;
            if (clawhubPart.includes("/")) {
              const slashIdx = clawhubPart.indexOf("/");
              ownerHandle = clawhubPart.substring(0, slashIdx);
              slug = clawhubPart.substring(slashIdx + 1);
            }
            ctx.addItem(makeItem(ctx.sessionId, "info", `Installing from ClawHub: ${slug}`));
            const downloadPayload = await ctx.request<{ success?: boolean; detail?: string; detail_key?: string; skill?: { name?: string; source?: string } }>(
              "skills.clawhub.download",
              { slug, owner_handle: ownerHandle || "", force: false },
              120_000,
            );
            if (downloadPayload.success) {
              const skillName = downloadPayload.skill?.name || slug;
              ctx.addItem(makeItem(ctx.sessionId, "info", `Skill installed from ClawHub: ${skillName}`));
            } else {
              // Token not configured — give actionable guidance
              if (downloadPayload.detail_key === "skills.clawhub.errors.tokenNotConfigured") {
                ctx.addItem(makeItem(ctx.sessionId, "error",
                  `ClawHub token not configured. Please set your token first:\n` +
                  `  /skills marketplace clawhub token <your-token>\n` +
                  `Get your token at: https://clawhub.ai`));
                return;
              }
              // Skill already installed — ask user if they want to force overwrite
              if (downloadPayload.detail_key === "skills.clawhub.errors.skillAlreadyInstalled"
                  || downloadPayload.detail?.includes("已安装") || downloadPayload.detail?.includes("已存在")) {
                const answers = await ctx.askQuestions([
                  {
                    header: "ClawHub",
                    question: `Skill "${slug}" is already installed. Do you want to force overwrite it?`,
                    options: [
                      { label: "Yes, overwrite", description: `Re-install "${slug}" from ClawHub, replacing the existing version` },
                      { label: "No, cancel", description: "Keep the existing skill unchanged" },
                    ],
                  },
                ]);
                const selected = answers[0]?.selected_options?.[0];
                if (selected === "Yes, overwrite") {
                  ctx.addItem(makeItem(ctx.sessionId, "info", `Force re-installing from ClawHub: ${slug}`));
                  const forcePayload = await ctx.request<{ success?: boolean; detail?: string; detail_key?: string; skill?: { name?: string; source?: string } }>(
                    "skills.clawhub.download",
                    { slug, owner_handle: ownerHandle || "", force: true },
                    120_000,
                  );
                  if (forcePayload.success) {
                    const skillName = forcePayload.skill?.name || slug;
                    ctx.addItem(makeItem(ctx.sessionId, "info", `Skill re-installed from ClawHub: ${skillName}`));
                  } else {
                    ctx.addItem(makeItem(ctx.sessionId, "error", forcePayload.detail || `ClawHub force install failed: ${slug}`));
                  }
                } else {
                  ctx.addItem(makeItem(ctx.sessionId, "info", `Installation cancelled. Skill "${slug}" remains unchanged.`));
                }
                return;
              }
              // Other download error — try searching ClawHub so user can find the correct slug
              ctx.addItem(makeItem(ctx.sessionId, "info", `Direct install failed for "${slug}", searching ClawHub for matching skills...`));
              try {
                const searchPayload = await ctx.request<{ success?: boolean; detail?: string; detail_key?: string; skills?: Array<{ slug: string; display_name: string; summary: string; version: string; updated_at: number }> }>(
                  "skills.clawhub.search",
                  { q: slug, limit: 10 },
                  60_000,
                );
                if (searchPayload.success && searchPayload.skills?.length) {
                  const items = searchPayload.skills.map((s) => ({
                    label: `${s.display_name || s.slug}`,
                    value: s.slug,
                    description: `${s.summary || "(no description)"} | slug: ${s.slug} | v${s.version || "?"}`,
                  }));
                  ctx.addItem(makeItem(ctx.sessionId, "info", `Found ${searchPayload.skills.length} matching skills on ClawHub. Use the slug shown below:`, "*", { view: "list", title: "ClawHub Search Results", items }));
                } else {
                  // Search also requires token — if token error, give guidance
                  if (searchPayload.detail_key === "skills.clawhub.errors.tokenNotConfigured") {
                    ctx.addItem(makeItem(ctx.sessionId, "error",
                      `ClawHub token not configured. Please set your token first:\n` +
                      `  /skills marketplace clawhub token <your-token>\n` +
                      `Get your token at: https://clawhub.ai`));
                  } else {
                    ctx.addItem(makeItem(ctx.sessionId, "error", downloadPayload.detail || `ClawHub install failed: ${slug}`));
                  }
                }
              } catch {
                ctx.addItem(makeItem(ctx.sessionId, "error", downloadPayload.detail || `ClawHub install failed: ${slug}`));
              }
            }
            return;
          }

          // SkillNet install flow: "skill_name@skillnet"
          // SkillNet skills are identified by URL, not slug. The @skillnet format triggers
          // a search first. Only auto-installs if an exact match by skill_name is found;
          // otherwise shows search results and lets the user pick the right one.
          if (spec.endsWith("@skillnet")) {
            const skillName = spec.replace(/@skillnet$/i, "");
            ctx.addItem(makeItem(ctx.sessionId, "info", `Searching SkillNet for: ${skillName}`));
            const searchPayload = await ctx.request<{ success?: boolean; detail?: string; detail_key?: string; count?: number; skills?: SkillNetItem[] }>(
              "skills.skillnet.search",
              { q: skillName, limit: 10 },
              60_000,
            );
            if (!searchPayload.success || !searchPayload.skills?.length) {
              ctx.addItem(makeItem(ctx.sessionId, "error",
                searchPayload.detail || `No matching skills found on SkillNet for: ${skillName}`));
              return;
            }
            // Find exact match by skill_name
            const exactMatch = searchPayload.skills.find((s) => s.skill_name === skillName);
            if (!exactMatch) {
              // No exact match — only show search results, do NOT auto-install
              const items = searchPayload.skills.map((s) => ({
                label: s.skill_name || "?",
                value: s.skill_url,
                description: `${s.skill_description || "(no description)"} | by ${s.author || "?"} | ⭐${s.stars || 0}`,
              }));
              ctx.addItem(makeItem(ctx.sessionId, "info",
                `No exact match for "${skillName}" on SkillNet. Found ${searchPayload.skills.length} related skills.\n` +
                `To install, use one of:\n` +
                `  /skills skillnet install <url>\n` +
                `  /skills install <exact_skill_name>@skillnet`, "*", { view: "list", title: "SkillNet Search Results", items }));
              return;
            }
            // Exact match found — proceed with install
            const skillUrl = exactMatch.skill_url;
            ctx.addItem(makeItem(ctx.sessionId, "info", `Installing from SkillNet: ${exactMatch.skill_name}`));
            const installPayload = await ctx.request<{ success?: boolean; detail?: string; pending?: boolean; install_id?: string }>(
              "skills.skillnet.install",
              { url: skillUrl, force: false },
              120_000,
            );
            if (!installPayload.success) {
              // SkillNet install is async: "already installed" is normally detected during polling,
              // but handle the sync response defensively before falling back to a plain error.
              if (await promptSkillNetOverwrite(ctx, skillUrl, installPayload)) return;
              ctx.addItem(makeItem(ctx.sessionId, "error", installPayload.detail || `SkillNet install failed: ${skillUrl}`));
              return;
            }
            // Async install — poll for completion. "Already installed" is detected here by the
            // backend during the background job, surfaced as status=failed with a specific detail.
            if (installPayload.pending && installPayload.install_id) {
              ctx.addItem(makeItem(ctx.sessionId, "info", `Downloading... (install_id: ${installPayload.install_id.slice(0, 8)})`));
              const finalSt = await pollSkillNetInstall(ctx, installPayload.install_id);
              if (finalSt.success && finalSt.status === "done") {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Skill installed from SkillNet: ${finalSt.skill?.name || exactMatch.skill_name}`));
              } else if (await promptSkillNetOverwrite(ctx, skillUrl, finalSt)) {
                return;
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "error", finalSt.detail || `SkillNet install failed: ${skillUrl}`));
              }
            } else {
              ctx.addItem(makeItem(ctx.sessionId, "info", `Skill installed from SkillNet: ${exactMatch.skill_name}`));
            }
            return;
          }

          // Marketplace / builtin install flow
          let finalSpec = spec;
          // Bare skill name without @ — auto-detect if it's a builtin
          if (!spec.includes("@")) {
            try {
              const payload = await ctx.request("skills.list", {});
              const skills = flattenArrayPayload(payload);
              const builtinMatch = skills.find((item) => {
                if (item && typeof item === "object") {
                  const obj = item as Record<string, unknown>;
                  return obj.name === spec
                    && (obj.is_builtin_source === true || obj.is_builtin === true);
                }
                return false;
              });
              if (builtinMatch) {
                finalSpec = `${spec}@builtin`;
              }
            } catch {
              // If list fails, let backend handle the bare name
            }
          }
          const payload = await ctx.request<{ success?: boolean; detail?: string }>(
            "skills.install",
            { spec: finalSpec, force: false },
            120_000,
          );
          if (payload.success) {
            ctx.addItem(makeItem(ctx.sessionId, "info", `Skill installed: ${finalSpec}`));
          } else if (payload.detail?.includes("已存在") || payload.detail?.includes("已安装")) {
            // Builtin skills cannot be force-overwritten (backend rejects force) — just warn.
            // A bare name (no @) or "<name>@builtin" both resolve to a builtin on the backend.
            const isBuiltin = !finalSpec.includes("@") || finalSpec.endsWith("@builtin");
            if (isBuiltin) {
              const builtinName = finalSpec.replace(/@builtin$/i, "");
              ctx.addItem(makeItem(ctx.sessionId, "info",
                `Skill "${builtinName}" is already installed (built-in). Reinstallation is not supported — the existing version is kept.`));
              return;
            }
            // Marketplace skill already installed — ask user if they want to force overwrite
            const answers = await ctx.askQuestions([
              {
                header: "Install",
                question: `Skill "${finalSpec}" is already installed. Do you want to force overwrite it?`,
                options: [
                  { label: "Yes, overwrite", description: `Re-install "${finalSpec}", replacing the existing version` },
                  { label: "No, cancel", description: "Keep the existing skill unchanged" },
                ],
              },
            ]);
            const selected = answers[0]?.selected_options?.[0];
            if (selected === "Yes, overwrite") {
              ctx.addItem(makeItem(ctx.sessionId, "info", `Force re-installing: ${finalSpec}`));
              const forcePayload = await ctx.request<{ success?: boolean; detail?: string }>(
                "skills.install",
                { spec: finalSpec, force: true },
                120_000,
              );
              if (forcePayload.success) {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Skill re-installed: ${finalSpec}`));
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "error", forcePayload.detail || `Install failed: ${finalSpec}`));
              }
            } else {
              ctx.addItem(makeItem(ctx.sessionId, "info", `Installation cancelled. Skill "${finalSpec}" remains unchanged.`));
            }
          } else {
            ctx.addItem(
              makeItem(ctx.sessionId, "error", payload.detail || `Install failed: ${finalSpec}`),
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
        description: "Manage marketplace sources (Git repos) and ClawHub token",
        usage: "/skills marketplace [list|add|remove|toggle|clawhub]",
        example: "/skills marketplace list  |  /skills marketplace clawhub token abc123",
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
          {
            name: "clawhub",
            description: "Manage ClawHub token (view or set)",
            usage: "/skills marketplace clawhub token [value]",
            example: "/skills marketplace clawhub token abc123  |  /skills marketplace clawhub",
            kind: CommandKind.BUILT_IN,
            takesArgs: true,
            action: async (ctx, args) => {
              const token = args.trim();
              if (!token) {
                // View current token status
                const payload = await ctx.request<{ success?: boolean; token?: string; has_token?: boolean }>(
                  "skills.clawhub.get_token",
                  {},
                );
                if (payload.success) {
                  if (payload.has_token) {
                    ctx.addItem(makeItem(ctx.sessionId, "info", `ClawHub token configured: ${payload.token}`));
                  } else {
                    ctx.addItem(makeItem(ctx.sessionId, "info",
                      `ClawHub token not configured.\n` +
                      `To set your token:\n` +
                      `  /skills marketplace clawhub token <your-token>\n` +
                      `Get your token at: https://clawhub.ai`));
                  }
                } else {
                  ctx.addItem(makeItem(ctx.sessionId, "error", "Failed to check ClawHub token status"));
                }
                return;
              }
              // Set token
              const payload = await ctx.request<{ success?: boolean; detail?: string; token?: string }>(
                "skills.clawhub.set_token",
                { token },
              );
              if (payload.success) {
                ctx.addItem(makeItem(ctx.sessionId, "info", `ClawHub token saved: ${payload.token || "(masked)"}`));
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || "Failed to save ClawHub token"));
              }
            },
            subCommands: [
              {
                name: "token",
                description: "Set or view ClawHub CLI token",
                usage: "/skills marketplace clawhub token [value]",
                example: "/skills marketplace clawhub token abc123  |  /skills marketplace clawhub token",
                kind: CommandKind.BUILT_IN,
                takesArgs: true,
                action: async (ctx, args) => {
                  const token = args.trim();
                  if (!token) {
                    const payload = await ctx.request<{ success?: boolean; token?: string; has_token?: boolean }>(
                      "skills.clawhub.get_token",
                      {},
                    );
                    if (payload.success) {
                      if (payload.has_token) {
                        ctx.addItem(makeItem(ctx.sessionId, "info", `ClawHub token configured: ${payload.token}`));
                      } else {
                        ctx.addItem(makeItem(ctx.sessionId, "info",
                          `ClawHub token not configured.\n` +
                          `To set your token:\n` +
                          `  /skills marketplace clawhub token <your-token>\n` +
                          `Get your token at: https://clawhub.ai`));
                      }
                    } else {
                      ctx.addItem(makeItem(ctx.sessionId, "error", "Failed to check ClawHub token status"));
                    }
                    return;
                  }
                  const payload = await ctx.request<{ success?: boolean; detail?: string; token?: string }>(
                    "skills.clawhub.set_token",
                    { token },
                  );
                  if (payload.success) {
                    ctx.addItem(makeItem(ctx.sessionId, "info", `ClawHub token saved: ${payload.token || "(masked)"}`));
                  } else {
                    ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || "Failed to save ClawHub token"));
                  }
                },
              },
            ],
          },
        ],
      },
      {
        name: "skillnet",
        description: "SkillNet online skill registry (search, install)",
        usage: "/skills skillnet [search|install]",
        example: "/skills skillnet search code  |  /skills skillnet install <url>",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const q = args.trim();
          if (!q) {
            ctx.addItem(makeItem(ctx.sessionId, "info",
              `SkillNet — online skill registry (OpenKG/ZJU).\n` +
              `Search:   /skills skillnet search <query>\n` +
              `Install:  /skills skillnet install <skill_url>\n` +
              `Or use:   /skills install <skill_name>@skillnet`));
            return;
          }
          // Default: search
          ctx.addItem(makeItem(ctx.sessionId, "info", `Searching SkillNet for: ${q}`));
          const payload = await ctx.request<{ success?: boolean; detail?: string; detail_key?: string; count?: number; skills?: SkillNetItem[] }>(
            "skills.skillnet.search",
            { q, limit: 20 },
            60_000,
          );
          if (payload.success && payload.skills?.length) {
            const items = payload.skills.map((s) => ({
              label: s.skill_name || "?",
              value: s.skill_url,
              description: `${s.skill_description || "(no description)"} | by ${s.author || "?"} | ⭐${s.stars || 0} | ${s.category || "-"}`,
            }));
            ctx.addItem(makeItem(ctx.sessionId, "info", `SkillNet results (${payload.skills.length})`, "*", { view: "list", title: "SkillNet Search Results (use /skills skillnet install <url> to install)", items }));
          } else {
            ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || `SkillNet search failed: ${q}`));
          }
        },
        subCommands: [
          {
            name: "search",
            description: "Search skills on SkillNet",
            usage: "/skills skillnet search <query>",
            example: "/skills skillnet search code-review",
            kind: CommandKind.BUILT_IN,
            takesArgs: true,
            action: async (ctx, args) => {
              const q = args.trim();
              if (!q) {
                ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills skillnet search <query>"));
                return;
              }
              ctx.addItem(makeItem(ctx.sessionId, "info", `Searching SkillNet for: ${q}`));
              const payload = await ctx.request<{ success?: boolean; detail?: string; detail_key?: string; count?: number; skills?: SkillNetItem[] }>(
                "skills.skillnet.search",
                { q, limit: 20 },
                60_000,
              );
              if (payload.success && payload.skills?.length) {
                const items = payload.skills.map((s) => ({
                  label: s.skill_name || "?",
                  value: s.skill_url,
                  description: `${s.skill_description || "(no description)"} | by ${s.author || "?"} | ⭐${s.stars || 0} | ${s.category || "-"}`,
                }));
                ctx.addItem(makeItem(ctx.sessionId, "info", `SkillNet results (${payload.skills.length})`, "*", { view: "list", title: "SkillNet Search Results (use /skills skillnet install <url> to install)", items }));
              } else if (payload.success) {
                // Search succeeded but matched nothing — not a failure
                ctx.addItem(makeItem(ctx.sessionId, "info", `No skills found on SkillNet for: ${q}`));
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "error", payload.detail || `SkillNet search failed: ${q}`));
              }
            },
          },
          {
            name: "install",
            description: "Install a skill from SkillNet by URL",
            usage: "/skills skillnet install <skill_url>",
            example: "/skills skillnet install https://github.com/user/skill-repo",
            kind: CommandKind.BUILT_IN,
            takesArgs: true,
            action: async (ctx, args) => {
              const url = args.trim();
              if (!url) {
                ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills skillnet install <skill_url>"));
                return;
              }
              ctx.addItem(makeItem(ctx.sessionId, "info", `Installing from SkillNet: ${url}`));
              const installPayload = await ctx.request<{ success?: boolean; detail?: string; pending?: boolean; install_id?: string }>(
                "skills.skillnet.install",
                { url, force: false },
                120_000,
              );
              if (!installPayload.success) {
                // SkillNet install is async: the sync response rarely carries "already installed"
                // directly, but handle it defensively before falling back to a plain error.
                if (await promptSkillNetOverwrite(ctx, url, installPayload)) return;
                ctx.addItem(makeItem(ctx.sessionId, "error", installPayload.detail || `SkillNet install failed: ${url}`));
                return;
              }
              // Async install — poll for completion. "Already installed" is detected here by the
              // backend during the background job, surfaced as status=failed with a specific detail.
              if (installPayload.pending && installPayload.install_id) {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Downloading... (install_id: ${installPayload.install_id.slice(0, 8)})`));
                const finalSt = await pollSkillNetInstall(ctx, installPayload.install_id);
                if (finalSt.success && finalSt.status === "done") {
                  ctx.addItem(makeItem(ctx.sessionId, "info", `Skill installed from SkillNet: ${finalSt.skill?.name || url}`));
                } else if (await promptSkillNetOverwrite(ctx, url, finalSt)) {
                  return;
                } else {
                  ctx.addItem(makeItem(ctx.sessionId, "error", finalSt.detail || `SkillNet install failed: ${url}`));
                }
              } else {
                ctx.addItem(makeItem(ctx.sessionId, "info", `Skill installed from SkillNet: ${url}`));
              }
            },
          },
        ],
      },
      {
        name: "use",
        description: "Use a skill to execute a query",
        usage: "/skills use <skill_name>, <query>",
        example: "/skills use my-skill, Code and execute a Hello World program.",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completionSuffix: ", ",
        completion: async (ctx, partial) => {
          const commaIndex = partial.indexOf(",");
          if (commaIndex !== -1 && partial.slice(0, commaIndex).trim()) return [];
          const filterTerm = commaIndex !== -1 ? "" : partial;
          try {
            const payload = await ctx.request("skills.list", {});
            const skills = flattenArrayPayload(payload)
              .map((item) => {
                if (item && typeof item === "object") {
                  const obj = item as Record<string, unknown>;
                  return typeof obj.name === "string" ? obj.name : null;
                }
                return null;
              })
              .filter((name): name is string => name !== null);
            if (!filterTerm) return skills;
            const lower = filterTerm.toLowerCase();
            return skills.filter((name) => name.toLowerCase().includes(lower));
          } catch {
            return [];
          }
        },
        action: async (ctx, args) => {
          const parts = args.trim().split(/\s*,\s*(.*)/);
          const skill_name = parts[0]?.trim();
          const query = parts[1]?.trim();
          if (!skill_name || !query) {
            ctx.addItem(makeItem(ctx.sessionId, "error", "Usage: /skills use <skill_name>, <query>"));
            return;
          }
          // 兼容路径：不再拼 `/skills use` 文本，
          // 直接把干净的 query 作为 content 发送，skill 名走独立的 skills 参数。
          const requestId = ctx.sendMessage(query, undefined, undefined, undefined, [skill_name]);
          if (!requestId) {
            ctx.addItem(
              makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending /skills use request"),
            );
            return;
          }
        },
      },
    ],
  };
}
