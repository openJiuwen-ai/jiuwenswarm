import { addError, addInfo } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

function showCurrent(ctx: CommandContext): void {
  const v = ctx.getWorkspaceDir();
  ctx.addItem(
    addInfo(ctx.sessionId, v ? "Workspace directory" : "Workspace directory (not set)", "c", {
      view: "kv",
      title: "workspace_dir",
      items: [{ label: "path", value: v || "(empty)" }],
    }),
  );
}

export function createWorkspaceDirCommand(): SlashCommand {
  return {
    name: "workspace_dir",
    altNames: ["workspace-dir"],
    description: "Set or show workspace directory (sent with each chat to gateway)",
    usage: "/workspace_dir [get|set <path>|clear]",
    example: "/workspace_dir set C:\\Projects\\my-app",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    subCommands: [
      {
        name: "set",
        description: "Save workspace directory for this TUI",
        usage: "/workspace_dir set <path>",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: async (ctx, args) => {
          const path = args.trim();
          if (!path) {
            ctx.addItem(addError(ctx.sessionId, "usage: /workspace_dir set <path>"));
            return;
          }
          ctx.setWorkspaceDir(path);
          ctx.addItem(addInfo(ctx.sessionId, `Workspace directory set: ${path}`, "c"));
        },
      },
      {
        name: "get",
        description: "Show saved workspace directory",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          showCurrent(ctx);
        },
      },
      {
        name: "clear",
        description: "Clear saved workspace directory",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          ctx.setWorkspaceDir("");
          ctx.addItem(addInfo(ctx.sessionId, "Workspace directory cleared.", "c"));
        },
      },
    ],
    action: async (ctx, args) => {
      if (!args.trim()) {
        showCurrent(ctx);
        return;
      }
      ctx.addItem(
        addError(
          ctx.sessionId,
          "usage: /workspace_dir [get|set <path>|clear] — use subcommands, or /workspace_dir alone to show current",
        ),
      );
    },
  };
}
