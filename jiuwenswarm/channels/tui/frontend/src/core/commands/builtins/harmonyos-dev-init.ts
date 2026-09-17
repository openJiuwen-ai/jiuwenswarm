import { addError, addInfo } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

type RuntimeCheck = {
  ok?: boolean;
  path?: string | null;
  version?: string | null;
  error?: string | null;
  major?: number | null;
  minimumMajor?: number;
  supported?: boolean;
};

type CommandResult = {
  ok?: boolean;
  skipped?: boolean;
  requiresConfirmation?: boolean;
  reason?: string;
  error?: string | null;
  returncode?: number | null;
  stdout?: string;
  stderr?: string;
  command?: string[];
};

type KnowledgeMcpConfig = {
  name: string;
  enabled: boolean;
  transport: "streamable-http";
  url: string;
  timeout_s?: number;
};

type KnowledgeMcpStatus =
  | "available"
  | "configured"
  | "already_configured"
  | "declined"
  | "disabled"
  | "blocked"
  | "conflict";

type KnowledgeMcpReport = {
  status?: KnowledgeMcpStatus;
  config?: KnowledgeMcpConfig;
  expectedTools?: string[];
  toolCount?: number;
  tools?: string[];
  error?: string | null;
};

type HarmonyDevReport = {
  ok?: boolean;
  needsConfirmation?: boolean;
  needsUpdateConfirmation?: boolean;
  runtime?: {
    devecocli?: RuntimeCheck;
    node?: RuntimeCheck;
    npm?: RuntimeCheck;
  };
  actions?: {
    skillsPath?: string;
    skillsPathSource?: string;
    installDevecocliAttempted?: boolean;
    updateDevecocliAttempted?: boolean;
    initSkillAttempted?: boolean;
    installSuiteAttempted?: boolean;
    installDevecocli?: CommandResult;
    updateDevecocli?: CommandResult;
    initSkill?: CommandResult;
    installSuite?: CommandResult & {
      name?: string;
      sourcePath?: string;
      targetPath?: string;
      alreadyInstalled?: boolean;
      updated?: boolean;
      managed?: boolean;
      version?: string;
      sourceDigest?: string;
      conflict?: boolean;
    };
  };
  skillVerification?: {
    checked?: boolean;
    ok?: boolean;
    skillsPath?: string;
    skillCount?: number;
    skillFiles?: string[];
    newSkillFiles?: string[];
    baseSkillFound?: boolean;
    suiteSkillFound?: boolean;
    reason?: string;
  };
  knowledgeMcp?: KnowledgeMcpReport;
};

type McpListPayload = {
  items?: Array<{
    name?: string;
    enabled?: boolean;
    transport?: string;
    url?: string;
  }>;
};

type McpUpdatePayload = {
  applied?: boolean;
  error?: string;
};

type McpToolsPayload = {
  tools?: Array<{ name?: string }>;
};

const DEV_INIT_TIMEOUT_MS = 7 * 60 * 1000;
const DEV_INIT_CANCEL_TIMEOUT_MS = 20 * 1000;
const DEV_INIT_PROGRESS_INTERVAL_MS = 30 * 1000;
const DEV_INIT_INTERRUPT_POLL_MS = 250;
let devInitOperationSequence = 0;

type DevInitProgressMode = "install" | "update" | "skills";

function createDevInitOperationId(): string {
  devInitOperationSequence += 1;
  return `harmonyos-dev-init-${Date.now().toString(36)}-${devInitOperationSequence.toString(36)}`;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function statusText(check?: RuntimeCheck): string {
  if (!check) return "not checked";
  if (check.supported === false) return check.error || "not supported";
  if (check.ok) {
    const version = check.version ? ` ${check.version}` : "";
    const path = check.path ? ` (${check.path})` : "";
    return `ok${version}${path}`;
  }
  return check.error || "not available";
}

function resultText(result?: CommandResult): string {
  if (!result) return "not run";
  if (result.skipped) return `skipped - ${result.reason || "no reason"}`;
  const status = result.ok ? "ok" : "failed";
  const code =
    result.returncode === undefined || result.returncode === null
      ? ""
      : ` code=${result.returncode}`;
  const detail = result.error ? ` - ${result.error}` : "";
  return `${status}${code}${detail}`;
}

function commandText(result?: CommandResult): string {
  const command = result?.command;
  return command && command.length > 0 ? command.join(" ") : "(not run)";
}

function formatElapsed(elapsedMs: number): string {
  const totalSeconds = Math.max(1, Math.floor(elapsedMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function progressText(mode: DevInitProgressMode, elapsedMs: number): string {
  const elapsed = formatElapsed(elapsedMs);
  if (mode === "install") {
    return (
      `HarmonyOS Dev initialization is still running (${elapsed} elapsed). ` +
      "The npm install phase stops automatically after 3 minutes; CLI and Skill verification " +
      "run afterward. Press Esc or Ctrl+C to cancel."
    );
  }
  if (mode === "update") {
    return (
      `devecocli update and Skill refresh are still running (${elapsed} elapsed). ` +
      "Press Esc or Ctrl+C to cancel."
    );
  }
  return `HarmonyOS Dev Skill initialization is still running (${elapsed} elapsed). Press Esc or Ctrl+C to cancel.`;
}

function showReport(ctx: CommandContext, report: HarmonyDevReport): void {
  const runtime = report.runtime || {};
  const actions = report.actions || {};
  const skillVerification = report.skillVerification || {};
  const knowledgeMcp = report.knowledgeMcp;
  ctx.addItem(
    addInfo(
      ctx.sessionId,
      report.ok ? "HarmonyOS Dev Skills initialized" : "HarmonyOS Dev init failed",
      "h",
      {
        view: "kv",
        title: "HarmonyOS Dev Init",
        items: [
          { label: "ok", value: report.ok ? "true" : "false" },
          {
            label: "skills_path",
            value: actions.skillsPath || skillVerification.skillsPath || "(unknown)",
          },
          { label: "skills_path_source", value: actions.skillsPathSource || "(unknown)" },
          { label: "devecocli", value: statusText(runtime.devecocli) },
          { label: "node", value: statusText(runtime.node) },
          { label: "npm", value: statusText(runtime.npm) },
          {
            label: "install_attempted",
            value: actions.installDevecocliAttempted ? "true" : "false",
          },
          { label: "install_result", value: resultText(actions.installDevecocli) },
          { label: "install_command", value: commandText(actions.installDevecocli) },
          {
            label: "update_attempted",
            value: actions.updateDevecocliAttempted ? "true" : "false",
          },
          { label: "update_result", value: resultText(actions.updateDevecocli) },
          { label: "update_command", value: commandText(actions.updateDevecocli) },
          { label: "init_attempted", value: actions.initSkillAttempted ? "true" : "false" },
          { label: "init_result", value: resultText(actions.initSkill) },
          { label: "init_command", value: commandText(actions.initSkill) },
          { label: "suite_attempted", value: actions.installSuiteAttempted ? "true" : "false" },
          { label: "suite_result", value: resultText(actions.installSuite) },
          { label: "suite_target", value: actions.installSuite?.targetPath || "(not installed)" },
          { label: "suite_version", value: actions.installSuite?.version || "(unknown)" },
          {
            label: "suite_digest",
            value: actions.installSuite?.sourceDigest || "(unverified)",
          },
          { label: "skill_verified", value: skillVerification.ok ? "true" : "false" },
          { label: "skill_count", value: String(skillVerification.skillCount ?? "(unknown)") },
          { label: "base_skill", value: skillVerification.baseSkillFound ? "verified" : "missing" },
          {
            label: "suite_skill",
            value: skillVerification.suiteSkillFound ? "verified" : "missing",
          },
          {
            label: "new_skill_files",
            value: skillVerification.newSkillFiles?.join(", ") || "(none)",
          },
          { label: "skill_verify_reason", value: skillVerification.reason || "(none)" },
          { label: "knowledge_mcp", value: knowledgeMcp?.status || "not offered" },
          { label: "knowledge_mcp_server", value: knowledgeMcp?.config?.name || "(none)" },
          { label: "knowledge_mcp_tools", value: knowledgeMcp?.tools?.join(", ") || "(none)" },
          { label: "knowledge_mcp_error", value: knowledgeMcp?.error || "(none)" },
        ],
      },
    ),
  );

  if (actions.installDevecocliAttempted) {
    ctx.addItem(
      addInfo(ctx.sessionId, "npm global install was attempted — see risks below", "h", {
        view: "kv",
        title: "Installation Risks",
        items: [
          { label: "network", value: "requires network access" },
          { label: "permissions", value: "global npm install may need elevated permissions" },
          { label: "version", value: "@latest tag has version drift risk" },
          { label: "impact", value: "failure does not block normal JiuwenSwarm functionality" },
        ],
      }),
    );
  }

  if (report.ok) {
    ctx.addItem(
      addInfo(
        ctx.sessionId,
        "Skill initialized. If new skills don't appear, run /reload-plugins or restart the TUI.",
        "h",
      ),
    );
  }

  if (
    knowledgeMcp?.status === "blocked" ||
    knowledgeMcp?.status === "conflict" ||
    knowledgeMcp?.status === "disabled"
  ) {
    ctx.addItem(
      addInfo(
        ctx.sessionId,
        "HarmonyOS Dev Skills are ready, but the optional official knowledge MCP needs attention. Use /mcp show harmonyos_developer_knowledge to inspect it.",
        "h",
      ),
    );
  }

  if (!report.ok) {
    ctx.addItem(
      addError(
        ctx.sessionId,
        "HarmonyOS Dev init did not complete. Check Node.js >= 18, npm/devecocli, and the Skill verification results above.",
      ),
    );
  }
}

async function runInit(
  ctx: CommandContext,
  options: {
    installDevecocliConfirmed?: boolean;
    updateDevecocliConfirmed?: boolean;
    skipDevecocliUpdate?: boolean;
  } = {},
): Promise<HarmonyDevReport> {
  const operationId = createDevInitOperationId();
  const progressMode: DevInitProgressMode | undefined = options.installDevecocliConfirmed
    ? "install"
    : options.updateDevecocliConfirmed
      ? "update"
      : options.skipDevecocliUpdate
        ? "skills"
        : undefined;
  const startedAt = Date.now();
  const requestPromise = ctx.request<HarmonyDevReport>(
    "harmonyos.dev_init",
    {
      operationId,
      installDevecocliConfirmed: options.installDevecocliConfirmed === true,
      updateDevecocliConfirmed: options.updateDevecocliConfirmed === true,
      skipDevecocliUpdate: options.skipDevecocliUpdate === true,
    },
    DEV_INIT_TIMEOUT_MS,
  );
  // If a local interrupt wins the race, the RPC rejects later when the backend
  // cancellation response arrives. Keep that rejection observed.
  void requestPromise.catch(() => undefined);

  let progressTimer: ReturnType<typeof setInterval> | undefined;
  if (progressMode) {
    progressTimer = setInterval(() => {
      ctx.addItem(addInfo(ctx.sessionId, progressText(progressMode, Date.now() - startedAt), "h"));
    }, DEV_INIT_PROGRESS_INTERVAL_MS);
  }

  let interruptTimer: ReturnType<typeof setInterval> | undefined;
  const interruptPromise = new Promise<never>((_resolve, reject) => {
    interruptTimer = setInterval(() => {
      if (ctx.isInterruptRequested?.()) {
        reject(new Error("cancelled by user"));
      }
    }, DEV_INIT_INTERRUPT_POLL_MS);
  });
  try {
    return await Promise.race([requestPromise, interruptPromise]);
  } catch (error) {
    let cleanupError: unknown;
    try {
      await ctx.request("harmonyos.dev_init_cancel", { operationId }, DEV_INIT_CANCEL_TIMEOUT_MS);
    } catch (cancelError) {
      cleanupError = cancelError;
    }
    if (ctx.isInterruptRequested?.()) {
      ctx.clearInterruptRequested();
    }
    if (cleanupError !== undefined) {
      throw new Error(
        `${errorText(error)}; background cancellation could not be confirmed: ${errorText(cleanupError)}`,
      );
    }
    throw error;
  } finally {
    if (progressTimer !== undefined) clearInterval(progressTimer);
    if (interruptTimer !== undefined) clearInterval(interruptTimer);
  }
}

async function confirmDevecocliInstall(
  ctx: CommandContext,
  report: HarmonyDevReport,
): Promise<boolean> {
  const installCommand = commandText(report.actions?.installDevecocli);
  try {
    const [answer] = await ctx.askQuestions(
      [
        {
          header: "Install DevEco CLI",
          question:
            "devecocli is not installed. Install it globally with npm?\n\n" +
            `Command: ${installCommand}\n` +
            "This requires network access, changes the global npm environment, and installs the latest published version.",
          options: [
            {
              label: "Install devecocli",
              description:
                "Run the displayed global npm install command, then continue initialization",
            },
            {
              label: "Cancel",
              description: "Do not install anything or initialize HarmonyOS Dev Skills",
            },
          ],
        },
      ],
      "harmonyos_dev_install_confirm",
    );
    return answer?.selected_options?.[0] === "Install devecocli";
  } catch {
    return false;
  }
}

async function confirmDevecocliUpdate(
  ctx: CommandContext,
  report: HarmonyDevReport,
): Promise<"update" | "skip" | "cancel"> {
  const updateCommand = commandText(report.actions?.updateDevecocli);
  const currentVersion = report.runtime?.devecocli?.version || "unknown";
  try {
    const [answer] = await ctx.askQuestions(
      [
        {
          header: "Update DevEco CLI",
          question:
            `Installed devecocli version: ${currentVersion}\n\n` +
            `Command: ${updateCommand}\n` +
            "Update devecocli before force-refreshing its bundled Skill? This requires network access and can change the global installation.",
          options: [
            {
              label: "Update devecocli",
              description: "Update to the latest version, verify it, then refresh the Skill",
            },
            {
              label: "Continue without updating",
              description: "Keep the installed CLI version and only force-refresh its Skill",
            },
          ],
        },
      ],
      "harmonyos_dev_update_confirm",
    );
    const selected = answer?.selected_options?.[0];
    if (selected === "Update devecocli") return "update";
    if (selected === "Continue without updating") return "skip";
    return "cancel";
  } catch {
    return "cancel";
  }
}

function canonicalMcpTransport(value: string | undefined): string {
  const transport = (value || "").trim().toLowerCase();
  return transport === "http" || transport === "streamable_http" ? "streamable-http" : transport;
}

function canonicalMcpUrl(value: string | undefined): string {
  return (value || "").trim().replace(/\/+$/, "");
}

async function confirmKnowledgeMcp(
  ctx: CommandContext,
  config: KnowledgeMcpConfig,
): Promise<boolean> {
  try {
    const [answer] = await ctx.askQuestions(
      [
        {
          header: "Official Knowledge MCP",
          question:
            "Configure Huawei's official HarmonyOS Developer Knowledge MCP?\n\n" +
            `Service: ${config.url}\n` +
            "Search terms and MCP requests will be sent to this Huawei-hosted remote service. It can be disabled or removed later with /mcp.",
          options: [
            {
              label: "Configure MCP",
              description:
                "Enable real-time search and full-text access to official HarmonyOS documentation",
            },
            {
              label: "Skip",
              description:
                "Keep the DevEco CLI and Skills setup without adding the remote knowledge service",
            },
          ],
        },
      ],
      "harmonyos_knowledge_mcp_confirm",
    );
    return answer?.selected_options?.[0] === "Configure MCP";
  } catch {
    return false;
  }
}

async function verifyKnowledgeMcp(
  ctx: CommandContext,
  report: KnowledgeMcpReport,
  successStatus: "configured" | "already_configured",
): Promise<KnowledgeMcpReport> {
  const config = report.config;
  if (!config) return { ...report, status: "blocked", error: "knowledge MCP config is missing" };
  try {
    const payload = await ctx.request<McpToolsPayload>(
      "command.mcp",
      { action: "list_tools", name: config.name },
      60_000,
    );
    const tools = (payload.tools || [])
      .map((tool) => tool.name || "")
      .filter((name) => name.length > 0);
    const expectedTools = report.expectedTools || [];
    const missingTools = expectedTools.filter((name) => !tools.includes(name));
    if (missingTools.length > 0) {
      return {
        ...report,
        status: "blocked",
        toolCount: tools.length,
        tools,
        error: `official knowledge MCP is missing expected tools: ${missingTools.join(", ")}`,
      };
    }
    return {
      ...report,
      status: successStatus,
      toolCount: tools.length,
      tools,
      error: null,
    };
  } catch (error) {
    return {
      ...report,
      status: "blocked",
      toolCount: 0,
      tools: [],
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

async function configureKnowledgeMcp(
  ctx: CommandContext,
  report: KnowledgeMcpReport,
): Promise<KnowledgeMcpReport> {
  const config = report.config;
  if (!config) return report;

  try {
    const listed = await ctx.request<McpListPayload>("command.mcp", { action: "list" }, 60_000);
    const existing = (listed.items || []).find((item) => item.name === config.name);
    if (existing) {
      const compatible =
        canonicalMcpTransport(existing.transport) === config.transport &&
        canonicalMcpUrl(existing.url) === canonicalMcpUrl(config.url);
      if (!compatible) {
        return {
          ...report,
          status: "conflict",
          error: `an MCP server named ${config.name} already exists with different settings; it was not overwritten`,
        };
      }
      if (existing.enabled === false) {
        return {
          ...report,
          status: "disabled",
          error: "the official knowledge MCP is configured but disabled; enable it with /mcp",
        };
      }
      return verifyKnowledgeMcp(ctx, report, "already_configured");
    }

    const confirmed = await confirmKnowledgeMcp(ctx, config);
    if (!confirmed) return { ...report, status: "declined", error: null };

    const added = await ctx.request<McpUpdatePayload>(
      "command.mcp",
      { action: "add", ...config },
      60_000,
    );
    if (added.applied === false) {
      return {
        ...report,
        status: "blocked",
        error: added.error || "knowledge MCP configuration was saved but could not be applied",
      };
    }
    return verifyKnowledgeMcp(ctx, report, "configured");
  } catch (error) {
    return {
      ...report,
      status: "blocked",
      error: error instanceof Error ? error.message : String(error),
    };
  }
}

export function createHarmonyOSDevInitCommand(): SlashCommand {
  return {
    name: "harmonyos-dev-init",
    description: "Install devecocli if needed and initialize HarmonyOS Dev Skills",
    usage: "/harmonyos-dev-init",
    example: "/harmonyos-dev-init",
    kind: CommandKind.BUILT_IN,
    takesArgs: false,
    action: async (ctx) => {
      try {
        ctx.addItem(
          addInfo(ctx.sessionId, "Checking devecocli and HarmonyOS Dev prerequisites...", "h"),
        );
        let report = await runInit(ctx);
        if (report.needsConfirmation) {
          const confirmed = await confirmDevecocliInstall(ctx, report);
          if (!confirmed) {
            ctx.addItem(
              addInfo(
                ctx.sessionId,
                "HarmonyOS Dev init cancelled. devecocli was not installed.",
                "h",
              ),
            );
            return;
          }
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              "Installing devecocli (maximum 3 minutes), then initializing Skills. " +
                "Progress is reported every 30 seconds; press Esc or Ctrl+C to cancel.",
              "h",
            ),
          );
          report = await runInit(ctx, { installDevecocliConfirmed: true });
        }
        if (report.needsUpdateConfirmation) {
          const choice = await confirmDevecocliUpdate(ctx, report);
          if (choice === "cancel") {
            ctx.addItem(
              addInfo(
                ctx.sessionId,
                "HarmonyOS Dev init cancelled. devecocli was not updated and Skills were not changed.",
                "h",
              ),
            );
            return;
          }
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              choice === "update"
                ? "Updating devecocli and force-refreshing Skills..."
                : "Keeping the current devecocli version and force-refreshing Skills...",
              "h",
            ),
          );
          report = await runInit(ctx, {
            updateDevecocliConfirmed: choice === "update",
            skipDevecocliUpdate: choice === "skip",
          });
        }
        if (report.ok && report.knowledgeMcp) {
          report.knowledgeMcp = await configureKnowledgeMcp(ctx, report.knowledgeMcp);
        }
        showReport(ctx, report);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `harmonyos-dev-init failed: ${message}`));
      }
    },
  };
}
