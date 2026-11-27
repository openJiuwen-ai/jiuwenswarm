import { makeItem, parseArgs } from "../helpers.js";
import { CommandKind, type SlashCommand } from "../types.js";

/**
 * /evolve - Trigger skill evolution or list pending summaries
 * Usage: /evolve [list | <skill_name>]
 */
export function createEvolveCommand(): SlashCommand {
  return {
    name: "evolve",
    description: "Trigger skill evolution for <skill_name>, or list pending summaries if 'list' or no argument",
    usage: "/evolve [list | <skill_name>]",
    example: "/evolve pptx",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const skillArg = args.trim();
      // /evolve, /evolve list, or /evolve <skill_name>
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
 * Usage: /evolve_simplify <skill_name> [--dry-run]
 */
export function createEvolveSimplifyCommand(): SlashCommand {
  return {
    name: "evolve_simplify",
    description: "Simplify evolution experiences for a skill into smaller tasks",
    usage: "/evolve_simplify <skill_name> [--dry-run]",
    example: "/evolve_simplify pptx --dry-run",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const parsedArgs = parseArgs(args);
      const skillName = parsedArgs[0];

      if (!skillName || skillName.startsWith("--")) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /evolve_simplify <skill_name> [--dry-run] - Provide the name of the skill",
          ),
        );
        return;
      }

      // Forward all arguments to backend (including --dry-run if present)
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
 * /solidify - Approve and solidify evolution proposals for a skill
 * Usage: /solidify <skill_name>
 */
export function createSolidifyCommand(): SlashCommand {
  return {
    name: "solidify",
    description: "Approve and solidify evolution experiences for a skill into SKILL.md",
    usage: "/solidify <skill_name>",
    example: "/solidify pptx",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    action: (ctx, args) => {
      const skillName = args.trim();

      if (!skillName) {
        ctx.addItem(
          makeItem(
            ctx.sessionId,
            "error",
            "usage: /solidify <skill_name> - Provide the name of the skill",
          ),
        );
        return;
      }

      const requestId = ctx.sendMessage(`/solidify ${skillName}`);
      if (!requestId) {
        ctx.addItem(
          makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending solidify request"),
        );
      }
    },
  };
}
