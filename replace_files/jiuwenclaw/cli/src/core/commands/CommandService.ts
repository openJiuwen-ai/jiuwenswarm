import type { CommandContext, CommandSuggestion, SlashCommand } from "./types.js";
import { makeItem, parseArgs } from "./helpers.js";

export function parseSlashCommand(raw: string, commands: readonly SlashCommand[]) {
  const trimmed = raw.trim();
  const parts = trimmed.substring(1).trim().split(/\s+/).filter(Boolean);
  let currentCommands = commands;
  let command: SlashCommand | undefined;
  let parentCommand: SlashCommand | undefined;
  let pathIndex = 0;
  const canonicalPath: string[] = [];

  for (const part of parts) {
    let found = currentCommands.find((candidate) => candidate.name === part);
    if (!found) {
      found = currentCommands.find((candidate) => candidate.altNames?.includes(part));
    }
    if (!found) break;
    parentCommand = command;
    command = found;
    canonicalPath.push(found.name);
    pathIndex += 1;
    if (found.subCommands) {
      currentCommands = found.subCommands;
    } else {
      break;
    }
  }

  const args = parts.slice(pathIndex).join(" ");
  if (command && command.takesArgs === false && args.length > 0 && parentCommand) {
    return {
      name: parentCommand.name,
      args: parts.slice(pathIndex - 1).join(" "),
      canonicalPath: canonicalPath.slice(0, -1),
      command: parentCommand,
    };
  }

  return {
    name: command?.name ?? parts[0] ?? "",
    args,
    canonicalPath,
    command,
  };
}

export class CommandService {
  private commands = new Map<string, SlashCommand>();
  private aliases = new Map<string, string>();
  private topLevelCommands: SlashCommand[] = [];

  register(commands: readonly SlashCommand[]): void {
    this.topLevelCommands = [...commands];
    for (const command of commands) {
      this.registerCommand(command);
    }
  }

  private registerCommand(command: SlashCommand): void {
    this.commands.set(command.name, command);
    for (const alias of command.altNames ?? []) {
      this.aliases.set(alias, command.name);
    }
    for (const subCommand of command.subCommands ?? []) {
      this.registerCommand(subCommand);
    }
  }

  resolve(name: string): SlashCommand | undefined {
    const target = this.aliases.get(name) ?? name;
    return this.commands.get(target);
  }

  getAll(): SlashCommand[] {
    return this.topLevelCommands
      .filter((command) => !command.hidden)
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  async execute(raw: string, ctx: CommandContext): Promise<void> {
    const parsed = parseSlashCommand(raw.trim(), this.getAll());
    const command = parsed.command;
    if (!command) {
      ctx.addItem(makeItem(ctx.sessionId, "error", `Unknown command: /${parsed.name || ""}`));
      return;
    }
    try {
      await command.action(ctx, parsed.args);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      ctx.addItem(makeItem(ctx.sessionId, "error", message));
    }
  }

  async getSuggestions(partial: string, ctx?: CommandContext): Promise<CommandSuggestion[]> {
    const normalized = partial.replace(/^\//, "").toLowerCase();
    const parts = parseArgs(normalized);

    if (parts.length > 1) {
      const command = this.resolve(parts[0] ?? "");
      if (command?.completion && ctx) {
        const values = await command.completion(ctx, parts.slice(1).join(" "));
        return values.map((value) => ({
          value: `/${command.name} ${value}`,
          description: command.description,
          usage: command.usage,
          example: command.example,
        }));
      }
    }

    return this.getAll()
      .flatMap((command) =>
        [command.name, ...(command.altNames ?? [])].map((alias) => ({ command, alias })),
      )
      .filter(({ alias }) => alias.startsWith(normalized))
      .map(({ command }) => ({
        value: `/${command.name}`,
        description: command.description,
        usage: command.usage,
        example: command.example,
      }))
      .filter(
        (item, index, self) =>
          self.findIndex((candidate) => candidate.value === item.value) === index,
      );
  }
}
