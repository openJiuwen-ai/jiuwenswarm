import { makeItem, parseArgs } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { ClientMode } from "../../modes.js";

const EVOLUTION_SUPPORTED_MODES = new Set<ClientMode>([
  "agent.plan",
  "team",
  "team.plan",
  "code.team",
]);

function unsupportedEvolutionModeMessage(mode: ClientMode): string | null {
  if (EVOLUTION_SUPPORTED_MODES.has(mode)) {
    return null;
  }
  return `${mode} 模式下演进功能不可用。`;
}

/**
 * /evolve - Trigger skill evolution
 * Usage: /evolve <skill_name> [<user_query>...]
 */
export function createEvolveCommand(): SlashCommand {
  return {
    name: "evolve",
    description: "Trigger skill evolution for <skill_name> (optionally with user_query)",
    usage: "/evolve <skill_name> [<user_query>...]",
    example: "/evolve pptx improve error handling",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: true,
    action: (ctx, args) => {
      const unsupportedMode = unsupportedEvolutionModeMessage(ctx.mode);
      if (unsupportedMode) {
        ctx.addItem(makeItem(ctx.sessionId, "error", unsupportedMode));
        return;
      }

      const skillArg = args.trim();
      // Forward as-is. Agent mode still accepts bare /evolve for a pending-record summary.
      const text = skillArg ? `/evolve ${skillArg}` : `/evolve`;
      const requestId = ctx.sendMessage(text);
      if (!requestId) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending evolve request"),
        );
      }
    },
  };
}

/**
 * /evolve_list - List evolution proposals for a skill with scores
 * Usage: /evolve_list <skill_name> [--sort score]
 */
export function createEvolveListCommand(): SlashCommand {
  return {
    name: "evolve_list",
    description: "List evolution experiences for a skill with scores",
    usage: "/evolve_list <skill_name> [--sort score]",
    example: "/evolve_list pptx --sort score",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: true,
    action: (ctx, args) => {
      const parsedArgs = parseArgs(args);
      const skillName = parsedArgs[0];

      if (!skillName || skillName.startsWith("--")) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /evolve_list <skill_name> [--sort score] - Provide the name of the skill",
          ),
        );
        return;
      }

      // Forward all arguments to backend (including --sort score if present)
      const requestId = ctx.sendMessage(`/evolve_list ${args.trim()}`);
      if (!requestId) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending evolve_list request"),
        );
      }
    },
  };
}

/**
 * /evolve_simplify - Simplify evolution proposals for a skill
 * Usage: /evolve_simplify <skill_name> [user_intent]
 */
export function createEvolveSimplifyCommand(): SlashCommand {
  return {
    name: "evolve_simplify",
    description: "Simplify evolution experiences for a skill into smaller tasks",
    usage: "/evolve_simplify <skill_name> [user_intent]",
    example: "/evolve_simplify pptx merge duplicate export-failure records",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: true,
    action: (ctx, args) => {
      const unsupportedMode = unsupportedEvolutionModeMessage(ctx.mode);
      if (unsupportedMode) {
        ctx.addItem(makeItem(ctx.sessionId, "error", unsupportedMode));
        return;
      }

      const parsedArgs = parseArgs(args);
      const skillName = parsedArgs[0];

      if (!skillName || skillName.startsWith("--")) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /evolve_simplify <skill_name> [user_intent] - Provide the name of the skill",
          ),
        );
        return;
      }

      // Forward all arguments to backend; trailing text is treated as cleanup intent.
      const requestId = ctx.sendMessage(`/evolve_simplify ${args.trim()}`);
      if (!requestId) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending evolve_simplify request"),
        );
      }
    },
  };
}

/**
 * /evolve_rollback - Rollback a skill to an archived SemVer pair
 * Usage: /evolve_rollback <skill_name> [version|latest]
 */
export function createEvolveRollbackCommand(): SlashCommand {
  return {
    name: "evolve_rollback",
    description: "Rollback a skill to an archived version (omit version to list)",
    usage: "/evolve_rollback <skill_name> [version|latest]",
    example: "/evolve_rollback pptx latest",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    takesArgs: true,
    action: (ctx, args) => {
      const unsupportedMode = unsupportedEvolutionModeMessage(ctx.mode);
      if (unsupportedMode) {
        ctx.addItem(makeItem(ctx.sessionId, "error", unsupportedMode));
        return;
      }

      const skillArg = args.trim();
      const text = skillArg ? `/evolve_rollback ${skillArg}` : `/evolve_rollback`;
      const requestId = ctx.sendMessage(text);
      if (!requestId) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "offline: waiting for reconnect before sending evolve_rollback request",
          ),
        );
      }
    },
  };
}
