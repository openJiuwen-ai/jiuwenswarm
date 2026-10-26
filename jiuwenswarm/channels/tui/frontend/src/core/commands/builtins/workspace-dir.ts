import { readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";

import { addError, addInfo } from "../helpers.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

/**
 * Suggest directory paths matching the partial input for tab-completion.
 * Used by /workspace add|set|remove subcommands.
 *
 * Handles: absolute paths (/Users/...), home-relative (~/...), cwd-relative (./..., ../...),
 * and bare names. Returns only directories (not files).
 */
function completeDirPath(partial: string): string[] {
  const trimmed = partial.trim();

  // Expand ~ to home directory
  let input = trimmed;
  if (input === "~") {
    input = homedir();
  } else if (input.startsWith("~/")) {
    input = join(homedir(), input.slice(2));
  }

  let searchDir: string;
  let prefixFilter: string;

  if (!input) {
    // Empty input → suggest cwd subdirectories (absolute paths)
    searchDir = process.cwd();
    prefixFilter = "";
  } else if (input.endsWith("/")) {
    // Ends with / → list contents of that directory
    searchDir = isAbsolute(input) ? input : resolve(process.cwd(), input);
    prefixFilter = "";
  } else {
    // Partial path → search parent dir, filter by basename
    const d = dirname(input);
    searchDir = isAbsolute(input) ? d : resolve(process.cwd(), d);
    prefixFilter = basename(input);
  }

  let entries;
  try {
    entries = readdirSync(searchDir, { withFileTypes: true });
  } catch {
    return [];
  }

  const results: string[] = [];
  for (const entry of entries) {
    // Skip hidden files/directories by default
    if (entry.name.startsWith(".")) continue;
    if (!entry.name.toLowerCase().startsWith(prefixFilter.toLowerCase())) continue;

    let isDir = entry.isDirectory();
    if (!isDir && entry.isSymbolicLink()) {
      try {
        isDir = statSync(join(searchDir, entry.name)).isDirectory();
      } catch {
        continue;
      }
    }
    if (!isDir) continue;

    const fullPath = join(searchDir, entry.name);
    // Return absolute paths with trailing / so the user can continue tab-completing
    results.push(fullPath + "/");
  }

  results.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
  return results;
}

function showAllTrustedPaths(ctx: CommandContext): void {
  const trustedDirs = ctx.getTrustedDirs();

  const items: Array<{ label: string; value: string }> = [];

  // Show system default workspace (fixed)
  items.push({
    label: "workspace (system)",
    value: "~/.jiuwenswarm/agent/jiuwenclaw_workspace",
  });

  // Show current project scope (always resolved absolute path)
  items.push({
    label: "project scope",
    value: ctx.getCurrentProjectDir(),
  });

  // Show trusted directories for this project only
  if (trustedDirs.length > 0) {
    trustedDirs.forEach((dir, index) => {
      items.push({
        label: `trusted[${index}]`,
        value: dir,
      });
    });
  } else {
    items.push({
      label: "trusted",
      value: "(none - using workspace only)",
    });
  }

  ctx.addItem(
    addInfo(ctx.sessionId, "Trusted paths for current project", "c", {
      view: "kv",
      title: "Trusted Paths",
      items,
    }),
  );
}

export function createWorkspaceCommand(): SlashCommand {
  return {
    name: "workspace",
    altNames: ["workspace_dir", "workspace-dir"],
    description: "Manage trusted directories for file operations",
    usage: "/workspace [get|add <path>|set <path>|remove <path>|clear]",
    example: "/workspace add ./",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    subCommands: [
      {
        name: "get",
        description: "Show all trusted paths (workspace + trusted directories)",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          showAllTrustedPaths(ctx);
        },
      },
      {
        name: "add",
        description: "Add a trusted directory (cwd by default)",
        usage: "/workspace add [path]",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: async (_ctx, partial) => completeDirPath(partial),
        action: async (ctx, args) => {
          const directoryPath = args.trim();
          // Default to cwd if no path specified
          const resolvedPath = directoryPath || process.cwd();
          if (!resolvedPath) {
            ctx.addItem(addError(ctx.sessionId, "usage: /workspace add [path]"));
            return;
          }
          const result = ctx.addTrustedDir(resolvedPath);
          if (result === "added") {
            // Sync to server-side permissions
            try {
              ctx.sendEventOnly("command.add_dir", {
              path: resolvedPath,
              remember: true
            });
            } catch (error) {
              // Ignore sync errors, still add locally
              console.warn("Failed to sync trusted directory to server:", error);
            }
            ctx.addItem(
              addInfo(ctx.sessionId, `Trusted directory added: ${resolvedPath}`, "c", {
                view: "kv",
                title: "Add Trusted Dir",
                items: [{ label: "path", value: resolvedPath }],
              }),
            );
          } else if (result === "exists") {
            ctx.addItem(addInfo(ctx.sessionId, `Path already set as trusted dir: ${resolvedPath}`, "c"));
          } else if (result === "not_found") {
            ctx.addItem(addError(ctx.sessionId, `Path does not exist: ${resolvedPath}`));
          } else if (result === "no_access") {
            ctx.addItem(addError(ctx.sessionId, `Permission denied: cannot access directory ${resolvedPath}`));
          } else {
            ctx.addItem(addError(ctx.sessionId, `Path is not a directory: ${resolvedPath}`));
          }
        },
      },
      {
        name: "set",
        description: "Switch project scope to a new directory",
        usage: "/workspace set <path>",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: async (_ctx, partial) => completeDirPath(partial),
        action: async (ctx, args) => {
          const rawPath = args.trim();
          if (!rawPath) {
            ctx.addItem(addError(ctx.sessionId, "usage: /workspace set <path>"));
            return;
          }

          // Validate path without modifying state
          const result = ctx.validateDirPath(rawPath);
          if (result === "not_found") {
            ctx.addItem(addError(ctx.sessionId, `Path does not exist: ${rawPath}`));
            return;
          }
          if (result === "invalid") {
            ctx.addItem(addError(ctx.sessionId, `Path is not a directory: ${rawPath}`));
            return;
          }
          if (result === "no_access") {
            ctx.addItem(addError(ctx.sessionId, `Permission denied: cannot access directory ${rawPath}`));
            return;
          }

          // Switch project scope to the new directory (absolute path)
          ctx.setCurrentProjectDir(rawPath);
          // Add the new directory itself as a trusted dir for this project
          ctx.addTrustedDir(rawPath);
          // Sync to server-side permissions
          try {
            ctx.sendEventOnly("command.add_dir", {
              path: ctx.getCurrentProjectDir(),
              remember: true
            });
          } catch (error) {
            console.warn("Failed to sync trusted directory to server:", error);
          }

          const projectDir = ctx.getCurrentProjectDir();
          const finalDirs = ctx.getTrustedDirs();
          ctx.addItem(
            addInfo(ctx.sessionId, `Project scope switched: ${projectDir}`, "c", {
              view: "kv",
              title: "Set Trusted Dir",
              items: [
                { label: "project scope", value: projectDir },
                ...finalDirs.map((dir, i) => ({ label: `trusted[${i}]`, value: dir })),
              ],
            }),
          );
        },
      },
      {
        name: "remove",
        description: "Remove a trusted directory",
        usage: "/workspace remove <path>",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        completion: async (_ctx, partial) => completeDirPath(partial),
        action: async (ctx, args) => {
          const directoryPath = args.trim();
          if (!directoryPath) {
            ctx.addItem(addError(ctx.sessionId, "usage: /workspace remove <path>"));
            return;
          }
          const removed = ctx.removeTrustedDir(directoryPath);
          if (removed) {
            ctx.addItem(addInfo(ctx.sessionId, `Trusted directory removed: ${directoryPath}`, "c"));
          } else {
            ctx.addItem(addInfo(ctx.sessionId, `Path not in trusted dirs: ${directoryPath}`, "c"));
          }
        },
      },
      {
        name: "clear",
        description: "Clear all trusted directories (will use default workspace only)",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        action: async (ctx) => {
          ctx.clearTrustedDirs();
          ctx.addItem(addInfo(ctx.sessionId, "Trusted directories cleared. Using default workspace only.", "c"));
        },
      },
    ],
    action: async (ctx, args) => {
      if (!args.trim()) {
        showAllTrustedPaths(ctx);
        return;
      }
      ctx.addItem(
        addError(
          ctx.sessionId,
          "usage: /workspace [get|add [path]|set <path>|remove <path>|clear] — use subcommands",
        ),
      );
    },
  };
}