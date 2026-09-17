import { CommandKind, type SlashCommand, type SlashCommandListProvider } from "../types.js";
import { makeItem } from "../helpers.js";

const COMMAND_GROUPS: Record<string, { name: string; commands: string[] }> = {
  core: {
    name: "Core",
    commands: ["help", "clear", "exit", "init", "simplify", "copy", "export", "review", "security-review"],
  },
  session: {
    name: "Session",
    commands: ["resume", "rename", "session", "compact", "sessions", "new", "recap"],
  },
  model: {
    name: "Model",
    commands: ["model", "theme", "color"],
  },
  mcp: {
    name: "MCP",
    commands: ["mcp"],
  },
  skills: {
    name: "Skills",
    commands: ["skills", "teamskills"],
  },
  config: {
    name: "Config",
    commands: ["config", "workspace", "diff", "plan", "permissions"],
  },
};

/** 获取命令所属分组 */
function getCommandGroup(commandName: string): string | null {
  for (const [groupKey, group] of Object.entries(COMMAND_GROUPS)) {
    if (group.commands.includes(commandName)) {
      return groupKey;
    }
  }
  return null;
}

export function createHelpCommand(getCommands: SlashCommandListProvider): SlashCommand {
  return {
    name: "help",
    description: "Show available commands",
    usage: "/help",
    example: "/help",
    kind: CommandKind.BUILT_IN,
    action: (ctx) => {
      const commands = getCommands().filter((command) => !command.hidden);

      const groupedCommands: Record<string, Array<{ label: string; value?: string; description: string }>> = {};
      const ungroupedCommands: Array<{ label: string; value?: string; description: string }> = [];

      for (const command of commands) {
        const item = {
          label: `/${command.name}`,
          value: command.usage?.replace(/^\/[^\s]+/, "").trim() || undefined,
          description: command.description,
        };

        const groupKey = getCommandGroup(command.name);
        if (groupKey) {
          if (!groupedCommands[groupKey]) {
            groupedCommands[groupKey] = [];
          }
          groupedCommands[groupKey].push(item);
        } else {
          ungroupedCommands.push(item);
        }
      }

      const groups = Object.keys(COMMAND_GROUPS)
        .filter((key) => groupedCommands[key]?.length > 0)
        .map((key) => ({
          name: COMMAND_GROUPS[key].name,
          items: groupedCommands[key],
        }));

      if (ungroupedCommands.length > 0) {
        groups.push({
          name: "Other",
          items: ungroupedCommands,
        });
      }

      ctx.addItem(
        makeItem(ctx.sessionId, "info", "Available commands", "?", {
          view: "help",
          title: "Slash Commands",
          groups,
          version: ctx.version,
        }),
      );
    },
  };
}