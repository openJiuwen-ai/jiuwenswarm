import { makeItem, parseArgs } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";
import type { ClientMode } from "../../modes.js";

const EVOLUTION_SUPPORTED_MODES = new Set<ClientMode>([
  // 单 agent plan：原 agent.plan 的等价新串。
  "agent.work.plan",
  // team 全系列：原集合 {team, team.plan, code.team} 在新 canonical 下覆盖
  // team.work.* / team.code.* 的 normal 与 plan 变体；只列 plan 会让
  // team.work.normal / team.code.normal 下 /evolve 被前端误拒（回归 #1）。
  "team.work.normal",
  "team.work.plan",
  "team.code.normal",
  "team.code.plan",
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
    requiresSkillEvolution: true,
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
    requiresSkillEvolution: true,
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
    requiresSkillEvolution: true,
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
 * /evolve_rebuild - Rebuild SKILL.md via followup execution
 * Usage: /evolve_rebuild <skill_name> [<user_query>...]
 */
export function createEvolveRebuildCommand(): SlashCommand {
  return {
    name: "evolve_rebuild",
    description: "Rebuild SKILL.md from archived history and evolution records",
    usage: "/evolve_rebuild <skill_name> [<user_query>...]",
    example: "/evolve_rebuild pptx improve error handling",
    kind: CommandKind.BUILT_IN,
    hidden: true,
    requiresSkillEvolution: true,
    takesArgs: true,
    action: (ctx, args) => {
      const parsedArgs = parseArgs(args);
      const skillName = parsedArgs[0];

      if (!skillName || skillName.startsWith("--")) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /evolve_rebuild <skill_name> [<user_query>...] - Provide the name of the skill",
          ),
        );
        return;
      }

      const requestId = ctx.sendMessage(`/evolve_rebuild ${args.trim()}`);
      if (!requestId) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending evolve_rebuild request"),
        );
      }
    },
  };
}
