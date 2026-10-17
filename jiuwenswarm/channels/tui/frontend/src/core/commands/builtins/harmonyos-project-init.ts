import { addError, addInfo } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";
import { switchMode } from "./mode.js";
import { completeDirPath, switchProjectScope } from "./workspace-dir.js";
import {
  buildHarmonyOSProjectInitPrompt,
  type HarmonyProjectContext,
  type RuntimeCheck,
} from "./harmonyos-project-init.prompts.js";

type ProjectInitReport = {
  ok?: boolean;
  context?: HarmonyProjectContext;
  runtime?: { devecocli?: RuntimeCheck };
  statePath?: string;
};

function pathError(ctx: CommandContext, rawPath: string): string | null {
  const result = ctx.validateDirPath(rawPath);
  if (result === "not_found") return `Path does not exist: ${rawPath}`;
  if (result === "invalid") return `Path is not a directory: ${rawPath}`;
  if (result === "no_access") return `Permission denied: cannot access directory ${rawPath}`;
  return null;
}

function showProjectReport(ctx: CommandContext, report: ProjectInitReport): void {
  const context = report.context ?? {};
  const project = context.project ?? {};
  const selected = context.selected ?? {};
  const runtime = report.runtime?.devecocli;
  const modules = context.modules ?? [];

  ctx.addItem(
    addInfo(ctx.sessionId, "HarmonyOS project initialized for the current TUI session", "h", {
      view: "kv",
      title: "HarmonyOS Project Init",
      items: [
        { label: "project", value: project.name || "(unknown)" },
        { label: "root", value: project.path || "(unknown)" },
        { label: "bundle_name", value: project.bundleName || "(unknown)" },
        { label: "mode", value: "agent.code.normal" },
        { label: "product", value: selected.product || "(select explicitly)" },
        { label: "module", value: selected.module || "(select explicitly)" },
        { label: "ability", value: selected.ability || "(select explicitly)" },
        {
          label: "modules",
          value:
            modules
              .map((item) => `${item.name || "?"}${item.type ? ` (${item.type})` : ""}`)
              .join(", ") || "(none)",
        },
        { label: "build_modes", value: context.buildModes?.join(", ") || "(none)" },
        {
          label: "devecocli",
          value: runtime?.ok
            ? `${runtime.version || "ok"} (${runtime.path || "PATH"})`
            : runtime?.error || "not available",
        },
        { label: "context_state", value: report.statePath || "(unknown)" },
        { label: "source_files", value: context.sourceFiles?.join(", ") || "(none)" },
      ],
    }),
  );

  const notices = [...(context.ambiguities ?? []), ...(context.warnings ?? [])];
  if (notices.length > 0) {
    ctx.addItem(
      addInfo(ctx.sessionId, `Project inspection notices (${notices.length})`, "h", {
        view: "list",
        title: "HarmonyOS Project Notices",
        items: notices.map((value, index) => ({ label: String(index + 1), value })),
      }),
    );
  }
}

export function createHarmonyOSProjectInitCommand(): SlashCommand {
  return {
    name: "harmonyos-project-init",
    description: "Inspect a HarmonyOS project and initialize the current TUI code session",
    usage: "/harmonyos-project-init [project-path]",
    example: "/harmonyos-project-init ~/MyHarmonyApp",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async (_ctx, partial) => completeDirPath(partial),
    action: async (ctx, args) => {
      const requestedPath = args.trim() || ctx.getCurrentProjectDir() || process.cwd();
      const validationError = pathError(ctx, requestedPath);
      if (validationError) {
        ctx.addItem(addError(ctx.sessionId, validationError));
        return;
      }

      try {
        ctx.addItem(addInfo(ctx.sessionId, `Inspecting HarmonyOS project: ${requestedPath}`, "h"));
        const report = await ctx.request<ProjectInitReport>(
          "harmonyos.project_init",
          { path: requestedPath },
          60_000,
        );
        const projectPath = report.context?.project?.path;
        if (!report.ok || !projectPath) {
          throw new Error("backend returned an incomplete HarmonyOS project report");
        }

        const switched = await switchMode(ctx, "agent.code.normal", { announce: false });
        if (!switched) {
          ctx.addItem(
            addInfo(
              ctx.sessionId,
              "Project inspection completed, but activation was cancelled because mode was not switched.",
              "h",
            ),
          );
          return;
        }
        const scope = switchProjectScope(ctx, projectPath);
        if (!scope.ok) {
          throw new Error(`failed to activate project workspace: ${scope.reason}`);
        }

        showProjectReport(ctx, report);
        const prompt = buildHarmonyOSProjectInitPrompt(
          report.context ?? {},
          report.runtime?.devecocli,
        );
        const requestId = ctx.sendMessage(prompt, undefined, "agent.code.normal", {
          logAsUser: false,
        });
        if (!requestId) {
          ctx.addItem(
            addError(
              ctx.sessionId,
              "Project scope was activated, but the initialization prompt was not sent because TUI is offline.",
            ),
          );
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        ctx.addItem(addError(ctx.sessionId, `harmonyos-project-init failed: ${message}`));
      }
    },
  };
}
