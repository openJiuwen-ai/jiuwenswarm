import { addInfo, addError } from "../helpers.js";
import { isTeamMode, type ClientMode } from "../../modes.js";
import { CommandKind, type SlashCommand } from "../types.js";

export type SwarmflowToggleTarget = "on" | "off";

export interface SwarmflowTogglePlan {
  /** Whether to call config.set. */
  writeConfig: boolean;
  /** Whether to switch to team mode (only when enabling from non-team). */
  switchToTeam: boolean;
  /** User-facing info message. */
  message: string;
}

function parseSwarmflowEnabled(payload: Record<string, unknown> | null): boolean | null {
  if (!payload) return null;
  const value = payload.enable_swarmflow;
  if (value === "true" || value === true) return true;
  if (value === "false" || value === false) return false;
  return null;
}

/** Pure toggle planner — mirrors design state matrix for tests and command action. */
export function planSwarmflowToggle(input: {
  target: SwarmflowToggleTarget;
  currentEnabled: boolean | null;
  mode: ClientMode | string;
}): SwarmflowTogglePlan {
  const enabling = input.target === "on";
  const wasTeamMode = isTeamMode(input.mode as ClientMode);
  const currentEnabled = input.currentEnabled;

  if (currentEnabled !== null && currentEnabled === enabling) {
    if (enabling) {
      if (wasTeamMode) {
        return {
          writeConfig: false,
          switchToTeam: false,
          message:
            "SwarmFlow is already on in team mode. No changes were made.",
        };
      }
      return {
        writeConfig: false,
        switchToTeam: true,
        message:
          "SwarmFlow is already on. Switched to team mode — the next workflow run uses the enabled setting.",
      };
    }
    if (wasTeamMode) {
      return {
        writeConfig: false,
        switchToTeam: false,
        message:
          "SwarmFlow is already off. Mode remains team. No changes were made.",
      };
    }
    return {
      writeConfig: false,
      switchToTeam: false,
      message:
        "SwarmFlow is already off. Current mode unchanged. No changes were made.",
    };
  }

  if (enabling) {
    if (!wasTeamMode) {
      return {
        writeConfig: true,
        switchToTeam: true,
        message:
          "swarmflow on. Switched to team mode — swarmflow activates on the next workflow run.",
      };
    }
    return {
      writeConfig: true,
      switchToTeam: false,
      message:
        "swarmflow on. The handler activates on the next new session — use /new to apply immediately.",
    };
  }

  if (wasTeamMode) {
    return {
      writeConfig: true,
      switchToTeam: false,
      message:
        "swarmflow off. Takes effect on the next new session — use /new to apply immediately. Use /mode to switch away from team if needed.",
    };
  }
  return {
    writeConfig: true,
    switchToTeam: false,
    message:
      "swarmflow off. Takes effect on the next team session — when you switch to team mode via /mode, swarmflow will not start.",
  };
}

export function createSwarmflowCommand(): SlashCommand {
  return {
    name: "swarmflow",
    description: "Toggle swarmflow human-in-the-loop mode (on/off) or show status",
    usage: "/swarmflow [on|off]",
    example: "/swarmflow on",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async () => ["on", "off"],
    action: async (ctx, args) => {
      const sub = args.trim().toLowerCase();

      if (!sub) {
        const modeLabel = ctx.mode ?? "unknown";
        const payload = await ctx
          .request<Record<string, unknown>>("config.get", {})
          .catch(() => null);
        const enabled = parseSwarmflowEnabled(payload) === true;
        ctx.addItem(
          addInfo(ctx.sessionId, `swarmflow: ${enabled ? "on" : "off"} · mode: ${modeLabel}`, "i"),
        );
        return;
      }

      if (sub !== "on" && sub !== "off") {
        ctx.addItem(addError(ctx.sessionId, `Unknown argument: ${JSON.stringify(sub)}. Use /swarmflow on|off`));
        return;
      }

      const target = sub as SwarmflowToggleTarget;
      const payload = await ctx
        .request<Record<string, unknown>>("config.get", {})
        .catch(() => null);
      const currentEnabled = parseSwarmflowEnabled(payload);
      const plan = planSwarmflowToggle({
        target,
        currentEnabled,
        mode: ctx.mode ?? "unknown",
      });

      if (plan.writeConfig) {
        try {
          await ctx.request("config.set", {
            enable_swarmflow: target === "on" ? "true" : "false",
          });
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          ctx.addItem(addError(ctx.sessionId, `config.set enable_swarmflow failed: ${message}`));
          return;
        }
      }

      if (plan.switchToTeam && !isTeamMode(ctx.mode)) {
        const nextMode = "team";
        ctx.setMode(nextMode);
        try {
          await ctx.request("mode.set", { mode: nextMode });
        } catch {
          // mode.set is best-effort (same as /mode command pattern).
        }
      }

      ctx.addItem(addInfo(ctx.sessionId, plan.message, "i"));
    },
  };
}
