import type { CommandContext, CommandSuggestion, SlashCommand } from "./types.js";
import { flattenArrayPayload, makeItem, parseArgs } from "./helpers.js";

export function parseSlashCommand(raw: string, commands: readonly SlashCommand[]) {
  const trimmed = raw.trim();
  const parts = trimmed.substring(1).trim().split(/\s+/).filter(Boolean);
  let currentCommands = commands;
  let command: SlashCommand | undefined;
  let parentCommand: SlashCommand | undefined;
  let pathIndex = 0;
  const canonicalPath: string[] = [];

  for (const part of parts) {
    const lower = part.toLowerCase();
    let found = currentCommands.find((candidate) => candidate.name.toLowerCase() === lower);
    if (!found) {
      found = currentCommands.find((candidate) => candidate.altNames?.some((alt) => alt.toLowerCase() === lower));
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

export interface InstalledSkillEntry {
  name: string;
  description: string;
}

export class CommandService {
  private commands = new Map<string, SlashCommand>();
  private aliases = new Map<string, string>();
  private topLevelCommands: SlashCommand[] = [];
  private installedSkills: InstalledSkillEntry[] = [];

  /**
   * Optional callback invoked whenever the installed-skills cache is successfully
   * refreshed.  The UI layer registers this to rebuild its autocomplete provider
   * so that `/<skillName>` shorthands appear in the command-name dropdown.
   */
  onInstalledSkillsChange?: (skills: readonly InstalledSkillEntry[]) => void;

  register(commands: readonly SlashCommand[]): void {
    this.topLevelCommands = [...commands];
    for (const command of commands) {
      this.registerCommand(command);
    }
  }

  private registerCommand(command: SlashCommand): void {
    this.commands.set(command.name, command);
    for (const alias of command.altNames ?? []) {
      this.aliases.set(alias.toLowerCase(), command.name);
    }
    for (const subCommand of command.subCommands ?? []) {
      this.registerCommand(subCommand);
    }
  }

  resolve(name: string): SlashCommand | undefined {
    const lower = name.toLowerCase();
    const target = this.aliases.get(lower) ?? lower;
    return this.commands.get(target);
  }

  getAll(includeHidden = false): SlashCommand[] {
    return this.topLevelCommands
      .filter((command) => includeHidden || !command.hidden)
      .sort((a, b) => a.name.localeCompare(b.name));
  }

  /**
   * Fetches the current installed-skill list from the backend via `ctx` and
   * stores it in `this.installedSkills`. Called on every `execute()` so that
   * the cache stays fresh without any extra wiring. This function is also
   * called by the first WebSocket connection. (From app-screen.ts)
   */
  async refreshSkills(
    ctx: CommandContext,
  ): Promise<void> {
    try {
      const payload = await ctx.request("skills.list", {});
      const skills = flattenArrayPayload(payload);
      this.installedSkills = skills.flatMap((item) => {
        if (item && typeof item === "object") {
          const obj = item as Record<string, unknown>;
          if (obj.installed === true && typeof obj.name === "string") {
            return [{
              name: obj.name as string,
              description: typeof obj.description === "string" ? obj.description : "",
            }];
          }
        }
        return [];
      });
      // Notify the UI so it can rebuild the autocomplete provider with the
      // fresh `/<skillName>` shorthands.
      this.onInstalledSkillsChange?.(this.installedSkills);
    } catch {
      // Keep the previous cache if the RPC fails.
    }
  }

  getInstalledSkills(): readonly InstalledSkillEntry[] {
    return this.installedSkills;
  }

  async execute(raw: string, ctx: CommandContext): Promise<void> {
    const parsed = parseSlashCommand(raw.trim(), this.getAll(true));
    const command = parsed.command;
    if (!command) {
      // 注：/<skill> 已在 app-screen.handleSubmit 的行首分流里落到普通消息分支
      //（content 原样发送 + 提取 skills_to_use），不再改写成 /skills use。
      // 能走到这里的说明第一个 token 既非注册命令也非已装 skill → 未知命令。
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

  // Note: Command suggestions use the pi-tui library from app-screen.ts. This function is currently unused.
  async getSuggestions(partial: string, ctx?: CommandContext): Promise<CommandSuggestion[]> {
    const normalized = partial.replace(/^\//, "").toLowerCase();
    const parts = parseArgs(normalized);

    if (parts.length > 1) {
      // Traverse command chain to find the deepest matching command with completion
      let currentCommands = this.getAll();
      let matchedCommand: SlashCommand | undefined;
      let matchedPath: string[] = [];
      let remainingParts = parts;

      for (const part of parts) {
        const found = currentCommands.find((cmd) =>
          cmd.name === part || (cmd.altNames && cmd.altNames.includes(part))
        );
        if (!found) break;

        matchedCommand = found;
        matchedPath.push(found.name);
        remainingParts = remainingParts.slice(1);

        if (found.subCommands) {
          currentCommands = found.subCommands;
        } else {
          break;
        }
      }

      // If we found a command with completion and have remaining args
      if (matchedCommand?.completion && ctx && remainingParts.length >= 0) {
        const completionInput = remainingParts.join(" ");
        const values = await matchedCommand.completion(ctx, completionInput);
        const prefix = matchedPath.join(" ");
        return values.map((value) => ({
          value: `/${prefix} ${value}`,
          description: matchedCommand!.description,
          usage: matchedCommand!.usage,
          example: matchedCommand!.example,
        }));
      }

      // If we're at a subcommand level but haven't matched the final command,
      // suggest available subcommands
      if (matchedCommand?.subCommands && remainingParts.length > 0) {
        const lastPart = remainingParts[remainingParts.length - 1] || "";
        const subCommandSuggestions = matchedCommand.subCommands
          .filter((sub) => sub.name.startsWith(lastPart) || !lastPart)
          .map((sub) => ({
            value: `/${matchedPath.join(" ")} ${sub.name}`,
            description: sub.description,
            usage: sub.usage,
            example: sub.example,
          }));
        if (subCommandSuggestions.length > 0) {
          return subCommandSuggestions;
        }
      }
    }

    return this.getAll()
      .flatMap((command) =>
        [command.name, ...(command.altNames ?? [])].map((alias) => ({ command, alias })),
      )
      .filter(({ alias }) => alias.toLowerCase().startsWith(normalized))
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
