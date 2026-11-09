import type { AutocompleteItem } from "@mariozechner/pi-tui";
import type { ClientMode } from "../../modes.js";
import { formatModeForDisplay, normalizeToClientMode } from "../../modes.js";
import { makeItem } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

interface ModeSetResult {
  mode?: unknown;
  changed: boolean;
}

function requestErrorCode(error: unknown): string | undefined {
  return error instanceof Error && "code" in error && typeof error.code === "string"
    ? error.code
    : undefined;
}

function isUncertainModeSetError(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  return (
    requestErrorCode(error) === "AGENT_SERVER_TIMEOUT" ||
    requestErrorCode(error) === "SERVICE_UNAVAILABLE" ||
    error.message.startsWith("request timeout: mode.set") ||
    error.message.includes("AgentServer request timed out") ||
    error.message === "request failed: AGENT_SERVER_TIMEOUT" ||
    error.message.includes("AgentServer WebSocket connection closed") ||
    error.message.startsWith("agent server request failed: ") ||
    error.message === "disconnected"
  );
}

async function readAuthoritativeMode(
  ctx: CommandContext,
  sessionId: string,
  isCurrentSession: () => boolean,
): Promise<ModeSetResult | null> {
  try {
    const state = await ctx.request<{ mode: string }>("mode.get", { session_id: sessionId });
    const resolved = normalizeToClientMode(state.mode);
    if (!resolved) throw new Error("mode.get returned an unsupported mode");
    return { mode: resolved, changed: false };
  } catch (readError) {
    if (!isCurrentSession()) return null;
    ctx.markModeUnconfirmed?.();
    const message = readError instanceof Error ? readError.message : String(readError);
    ctx.addItem(
      makeItem(sessionId, "error", `Mode switch unconfirmed: ${message}. Chat will wait for mode sync.`, "m"),
    );
    return null;
  }
}

/** Select a draft's mode locally; commit a saved Session before updating its mode. */
export async function switchMode(
  ctx: CommandContext,
  nextMode: ClientMode,
  options: { announce?: boolean } = {},
): Promise<boolean> {
  // Boot may replace the placeholder ID; pin the resolved Session for every request.
  let sessionId: string;
  try {
    sessionId = ctx.resolveModeSessionId
      ? await ctx.resolveModeSessionId()
      : ctx.sessionId;
  } catch (error) {
    if ((ctx.getCurrentSessionId?.() ?? ctx.sessionId) !== ctx.sessionId) return false;
    const message = error instanceof Error ? error.message : String(error);
    ctx.addItem(makeItem(ctx.sessionId, "error", `Mode switch failed: ${message}`, "m"));
    return false;
  }
  // Context values are snapshots, but UI setters affect the live Session.
  const isCurrentSession = (): boolean =>
    (ctx.getCurrentSessionId?.() ?? ctx.sessionId) === sessionId;
  if (!isCurrentSession()) return false;
  if (ctx.isCurrentSessionDraft?.()) {
    ctx.setMode(nextMode);
    ctx.markModeConfirmed?.();
    if (nextMode.endsWith(".plan")) ctx.markPlanEntryFromSlashCommand?.();
    if (options.announce !== false) {
      ctx.addItem(makeItem(sessionId, "info", `Mode set to ${formatModeForDisplay(nextMode)}`, "m"));
    }
    return true;
  }
  const currentMode = ctx.getCurrentMode?.() ?? ctx.mode;
  let committedMode: ClientMode = currentMode;
  try {
    let result: ModeSetResult;
    try {
      result = await ctx.request<ModeSetResult>("mode.set", {
        session_id: sessionId,
        mode: nextMode,
        expected_mode: currentMode,
      });
    } catch (error) {
      if (requestErrorCode(error) === "MODE_CONFLICT") {
        const state = await readAuthoritativeMode(ctx, sessionId, isCurrentSession);
        if (!state) return false;
        result = state;
      } else {
        if (!isUncertainModeSetError(error)) {
          throw error;
        }
        // The first commit may have succeeded even when its response was lost.
        try {
          result = await ctx.request<ModeSetResult>("mode.set", {
            session_id: sessionId,
            mode: nextMode,
            expected_mode: currentMode,
          });
        } catch {
          const state = await readAuthoritativeMode(ctx, sessionId, isCurrentSession);
          if (!state) return false;
          result = state;
        }
      }
    }
    const resolved = typeof result?.mode === "string" ? normalizeToClientMode(result.mode) : undefined;
    if (!resolved) {
      if (!isCurrentSession()) return false;
      ctx.markModeUnconfirmed?.();
      throw new Error("mode.set returned an unsupported mode; retry /mode or sync before chat");
    }
    committedMode = resolved;
  } catch (error) {
    if (!isCurrentSession()) return false;
    const message = error instanceof Error ? error.message : String(error);
    ctx.addItem(makeItem(sessionId, "error", `Mode switch failed: ${message}`, "m"));
    return false;
  }
  if (!isCurrentSession()) return false;
  ctx.setMode(committedMode);
  ctx.markModeConfirmed?.();
  if (committedMode !== nextMode) {
    ctx.addItem(
      makeItem(sessionId, "error", `Mode switch not applied; current mode is ${formatModeForDisplay(committedMode)}`, "m"),
    );
    return false;
  }
  if (nextMode.endsWith(".plan")) {
    ctx.markPlanEntryFromSlashCommand?.();
  }
  if (options.announce !== false) {
    ctx.addItem(makeItem(sessionId, "info", `Mode set to ${formatModeForDisplay(nextMode)}`, "m"));
  }
  return true;
}

const MODE_ALIASES: Record<string, ClientMode> = {
  // Bare role aliases (no third segment) — default to *.normal variant.
  agent: "agent.work.normal",
  code: "agent.code.normal",
  team: "team.work.normal",
  "agent.work": "agent.work.normal",
  "agent.code": "agent.code.normal",
  "team.work": "team.work.normal",
  "team.code": "team.code.normal",
  "team.normal": "team.work.normal",
  // Explicit variants.
  "agent.work.normal": "agent.work.normal",
  "agent.code.normal": "agent.code.normal",
  "team.work.normal": "team.work.normal",
  "team.code.normal": "team.code.normal",
  // Plan 入口：映射到对应的 *.plan canonical（与 modes.ts LEGACY_MODE_TO_NEW
  // 一致），让旧 muscle memory 的 /mode plan / agent.plan / code.plan 真正进入
  // plan，而不是静默降级到 *.normal。进入 plan 的显式标记见 switchMode。
  plan: "agent.work.plan",
  "agent.plan": "agent.work.plan",
  "code.plan": "agent.code.plan",
  "team.plan": "team.work.plan",
  "team.plan.normal": "team.work.plan",
  "team.plan.code": "team.code.plan",
  // Legacy aliases mapping onto the new canonical 8 values.
  "agent.fast": "agent.work.normal",
  "code.normal": "agent.code.normal",
  "code.team": "team.code.normal",
};

/** Resolve a user-facing `/mode` token to the canonical runtime mode. */
export function resolveModeTarget(requestedMode: string): ClientMode | undefined {
  return MODE_ALIASES[requestedMode.trim()];
}

/**
 * TUI `/mode` 树形展示；两段分组（agent / team），每段下挂 work / code 两项。
 * `.plan` 段统一走 `/plan` 命令对称退出，不出现在此候选词中。
 * pi-tui `AutocompleteItem` 不支持 `disabled`/`header`，组头 `value` 兜底为组首子项，
 * 即"选中组头 = 切组首"。
 */
export function buildModeAutocompleteItems(): AutocompleteItem[] {
  return [
    { value: "agent.work", label: "agent" },
    { value: "agent.work", label: "  agent.work" },
    { value: "agent.code", label: "  agent.code" },
    { value: "team.work", label: "team" },
    { value: "team.work", label: "  team.work" },
    { value: "team.code", label: "  team.code" },
  ];
}

export function createModeCommand(): SlashCommand {
  const directModes = [
    "agent.work",
    "agent.code",
    "team.work",
    "team.code",
  ] as const;

  return {
    name: "mode",
    description: "Switch chat mode",
    usage: "/mode <agent.work|agent.code|team.work|team.code>",
    example: "/mode agent.code",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => [...directModes],
    action: async (ctx, args) => {
      const requestedMode = args.trim();
      // 无参数时显示当前 mode
      if (!requestedMode) {
        const currentMode = ctx.mode ?? "unknown";
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "info",
            `Current mode: ${formatModeForDisplay(currentMode)}`,
            "m",
          ),
        );
        return;
      }
      const nextMode = resolveModeTarget(requestedMode);
      if (!nextMode) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /mode <agent.work|agent.code|team.work|team.code>",
          ),
        );
        return;
      }

      await switchMode(ctx, nextMode);
    },
  };
}
