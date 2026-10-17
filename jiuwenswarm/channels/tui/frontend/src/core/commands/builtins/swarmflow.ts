import { addInfo, addError } from "../helpers.js";
import { formatModeForDisplay, isTeamMode, type ClientMode } from "../../modes.js";
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

  // Toggle state unchanged — no config write for enable_swarmflow.
  if (currentEnabled !== null && currentEnabled === enabling) {
    if (enabling) {
      if (wasTeamMode) {
        return {
          writeConfig: false,
          switchToTeam: false,
          message: "Already on. No changes.",
        };
      }
      // Non-team + already on → switch to team mode (mode change is a change).
      return {
        writeConfig: false,
        switchToTeam: true,
        message: "Already on. Switched to team mode.",
      };
    }
    // target === off, already off
    if (wasTeamMode) {
      return {
        writeConfig: false,
        switchToTeam: false,
        message: "Already off. Mode remains team. No changes. Use /mode to leave team.",
      };
    }
    // Non-team + already off → swarmflow doesn't run here, off is a no-op.
    return {
      writeConfig: false,
      switchToTeam: false,
      message: "Not running in non-team mode. Use /mode team then /swarmflow off.",
    };
  }

  // Toggle state changes → write config.
  if (enabling) {
    if (!wasTeamMode) {
      return {
        writeConfig: true,
        switchToTeam: true,
        message: "SwarmFlow on. Switched to team mode. Use /new to apply.",
      };
    }
    return {
      writeConfig: true,
      switchToTeam: false,
      message: "SwarmFlow on. Use /new to apply.",
    };
  }

  // target === off, was on
  if (wasTeamMode) {
    return {
      writeConfig: true,
      switchToTeam: false,
      message: "SwarmFlow off. Use /new to apply. Use /mode to leave team.",
    };
  }
  // Non-team + was on → off is a no-op (swarmflow not running here).
  return {
    writeConfig: false,
    switchToTeam: false,
    message: "Not running in non-team mode. Use /mode team then /swarmflow off.",
  };
}

export function createSwarmflowCommand(): SlashCommand {
  return {
    name: "swarmflow",
    description: "Toggle swarmflow human-in-the-loop mode (on/off) or show status. Use --budget <tokens|none> to set or clear the team token ceiling.",
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

      // Status query: /swarmflow (no subcommand).
      if (!sub) {
        const modeLabel = formatModeForDisplay(ctx.mode ?? "unknown");
        const payload = await ctx
          .request<Record<string, unknown>>("config.get", {})
          .catch(() => null);
        const enabled = parseSwarmflowEnabled(payload) === true;
        const budgetValue = parseSwarmflowBudget(payload);
        const budgetInfo = ` · budget: ${budgetLabel(budgetValue)}`;
        // Non-team + on → annotate that swarmflow isn't running here.
        const stateLabel = enabled
          ? (isTeamMode(ctx.mode) ? "on" : "on (not running in non-team mode)")
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

      // Fetch current config once — toggle + budget both read from it.
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

      // --- Determine the desired new budget value (for config write + message) ---
      // newBudget: null = unbounded, number = ceiling, undefined = no budget op.
      let newBudget: BudgetValue | undefined;
      if (target === "on") {
        if (budget !== null) {
          newBudget = budget;
        } else if (clearBudget) {
          newBudget = null;
        }
      }

      // Whether the budget actually changes from its current value.
      const budgetChanged =
        newBudget !== undefined && newBudget !== currentBudget;

      // --- Write config ---
      // enable_swarmflow: write only when toggle changes (plan.writeConfig).
      // swarmflow_budget: write when toggle changes AND a budget op is present,
      //   OR when toggle is unchanged but budget op is present (pure budget change).
      const writeEnable = plan.writeConfig;
      const writeBudget =
        (budgetChanged) &&
        (plan.writeConfig
          ? true // toggle + budget together: write both in one call
          : target === "on" && (budget !== null || clearBudget)); // pure budget change

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

      // --- Switch mode (best-effort) ---
      if (plan.switchToTeam && !isTeamMode(ctx.mode)) {
        const nextMode: ClientMode = "team.work.normal";
        ctx.setMode(nextMode);
        try {
          await ctx.request("mode.set", { mode: nextMode });
        } catch {
          // mode.set is best-effort (same as /mode command pattern).
        }
      }

      // --- Build message ---
      // plan.message covers the pure-toggle case. When a budget op is present:
      //  - budget changed → strip "No changes.", append "Budget changed from X
      //    to Y." + "Use /new to apply." (budget only applies on a fresh session).
      //  - budget unchanged → strip "No changes." (the budget op confirms it),
      //    append "Budget remains <label>." No apply tail (no config change).
      //  - no budget op (newBudget === undefined) → keep plan.message as-is.
      let message = plan.message;
      if (target === "on" && newBudget !== undefined) {
        if (budgetChanged) {
          const budgetPart = `Budget changed from ${budgetLabel(currentBudget)} to ${budgetLabel(newBudget)}.`;
          const base = message.replace(/ No changes\.$/, "");
          if (base.endsWith("Use /new to apply.")) {
            message = `${base.slice(0, -"Use /new to apply.".length)}${budgetPart} Use /new to apply.`;
          } else {
            message = `${base} ${budgetPart} Use /new to apply.`;
          }
        } else {
          // Budget op present but value unchanged — state it, no config write.
          const budgetPart = `Budget remains ${budgetLabel(newBudget)}.`;
          const base = message.replace(/ No changes\.$/, "");
          message = `${base} ${budgetPart} No changes.`;
        }
      }
      ctx.addItem(addInfo(ctx.sessionId, message, "i"));
    },
  };
}
