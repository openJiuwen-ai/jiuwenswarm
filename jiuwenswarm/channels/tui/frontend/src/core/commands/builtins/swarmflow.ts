import { addInfo, addError } from "../helpers.js";
import { isTeamMode, type ClientMode } from "../../modes.js";
import { CommandKind, type SlashCommand } from "../types.js";

export type SwarmflowToggleTarget = "on" | "off";

/** Budget value: null = unbounded (none), number = token ceiling. */
type BudgetValue = number | null;

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

/** Parse swarmflow_budget from config payload → BudgetValue (null = unbounded). */
function parseSwarmflowBudget(payload: Record<string, unknown> | null): BudgetValue {
  if (!payload) return null;
  const raw = payload.swarmflow_budget;
  if (raw == null || raw === "") return null;
  const num = Number(raw);
  if (!Number.isFinite(num) || num <= 0) return null;
  return num;
}

/** "unbounded" or "N,NNN tokens" — for from/to display in change messages. */
function budgetLabel(budget: BudgetValue): string {
  return budget === null ? "unbounded" : `${budget.toLocaleString()} tokens`;
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
    description:
      "Toggle swarmflow human-in-the-loop mode (on/off) or show status. Use --budget <tokens|none> to set or clear the team token ceiling.",
    usage: "/swarmflow [on|off] [--budget <tokens|none>]",
    example: "/swarmflow on --budget 500000",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    completion: async (_ctx, remainingArgs: string) => {
      const trimmed = (remainingArgs ?? "").trim().toLowerCase();
      // After "on" is typed, offer "--budget" (value includes "on" so Tab keeps it)
      if (trimmed === "on") return ["on", "on --budget "];
      if (trimmed === "off" || /\boff\b/.test(trimmed)) return [];
      if (/--budget/i.test(trimmed)) return [];
      return ["on", "off"];
    },
    action: async (ctx, args) => {
      const hasBudgetFlag = /--budget/i.test(args);
      const clearBudget = /--budget\s+none/i.test(args);
      const budgetMatch = args.match(/--budget\s+(\d+)/i);
      const budget = budgetMatch ? parseInt(budgetMatch[1], 10) : null;
      const sub = args.replace(/--budget(\s+\S+)?/i, "").trim().toLowerCase();

      if (hasBudgetFlag && budget === null && !clearBudget) {
        ctx.addItem(addError(ctx.sessionId, "Usage: /swarmflow on --budget <tokens|none>"));
        return;
      }

      if (budget !== null && budget <= 0) {
        ctx.addItem(addError(ctx.sessionId, "Budget must be a positive integer"));
        return;
      }

      if (!sub) {
        const modeLabel = ctx.mode ?? "unknown";
        const payload = await ctx
          .request<Record<string, unknown>>("config.get", {})
          .catch(() => null);
        const enabled = parseSwarmflowEnabled(payload) === true;
        const budgetValue = parseSwarmflowBudget(payload);
        const budgetInfo = ` · budget: ${budgetLabel(budgetValue)}`;
        const stateLabel = enabled
          ? isTeamMode(ctx.mode) ? "on" : "on (not running in non-team mode)"
          : "off";
        ctx.addItem(
          addInfo(ctx.sessionId, `swarmflow: ${stateLabel} · mode: ${modeLabel}${budgetInfo}`, "i"),
        );
        return;
      }

      if (sub !== "on" && sub !== "off") {
        ctx.addItem(addError(ctx.sessionId, `Unknown argument: ${JSON.stringify(sub)}. Use /swarmflow on|off`));
        return;
      }

      const target = sub as SwarmflowToggleTarget;
      if (target === "off" && (budget !== null || clearBudget)) {
        ctx.addItem(addError(ctx.sessionId, "--budget is only valid with /swarmflow on"));
        return;
      }

      const payload = await ctx
        .request<Record<string, unknown>>("config.get", {})
        .catch(() => null);
      const currentEnabled = parseSwarmflowEnabled(payload);
      const currentBudget = parseSwarmflowBudget(payload);
      const plan = planSwarmflowToggle({
        target,
        currentEnabled,
        mode: ctx.mode ?? "unknown",
      });

      // newBudget: null = unbounded, number = ceiling, undefined = no budget op.
      let newBudget: BudgetValue | undefined;
      if (target === "on") {
        if (budget !== null) {
          newBudget = budget;
        } else if (clearBudget) {
          newBudget = null;
        }
      }

      const budgetChanged = newBudget !== undefined && newBudget !== currentBudget;
      const writeEnable = plan.writeConfig;
      const writeBudget =
        budgetChanged &&
        (plan.writeConfig
          ? true
          : target === "on" && (budget !== null || clearBudget));

      if (writeEnable || writeBudget) {
        try {
          const values: Record<string, string | number> = {};
          if (writeEnable) {
            values["enable_swarmflow"] = target === "on" ? "true" : "false";
          }
          if (writeBudget) {
            values["swarmflow_budget"] = newBudget === null ? "none" : String(newBudget);
          }
          await ctx.request("config.set", values);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          ctx.addItem(addError(ctx.sessionId, `config.set failed: ${message}`));
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

      let message = plan.message;
      if (target === "on" && newBudget !== undefined) {
        if (budgetChanged) {
          const budgetPart = `Budget changed from ${budgetLabel(currentBudget)} to ${budgetLabel(newBudget)}.`;
          const base = message
            .replace(/ No changes were made\.$/, "")
            .replace(/ No changes\.$/, "");
          if (/use \/new to apply/i.test(base)) {
            message = `${base.replace(/\s*—?\s*use \/new to apply immediately\./i, ".")} ${budgetPart} Use /new to apply.`;
          } else {
            message = `${base} ${budgetPart} Use /new to apply.`;
          }
        } else {
          const budgetPart = `Budget remains ${budgetLabel(newBudget)}.`;
          const base = message
            .replace(/ No changes were made\.$/, "")
            .replace(/ No changes\.$/, "");
          message = `${base} ${budgetPart} No changes.`;
        }
      }
      ctx.addItem(addInfo(ctx.sessionId, message, "i"));
    },
  };
}
