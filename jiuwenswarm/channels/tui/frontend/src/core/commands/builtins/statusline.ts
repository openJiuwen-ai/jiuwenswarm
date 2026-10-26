import { makeItem } from "../helpers.js";
import { loadTuiConfig, saveTuiConfig, type StatusLineSetting } from "../../tui-config-store.js";
import { CommandKind, type CommandContext, type SlashCommand } from "../types.js";

function getStatusLineConfig(): StatusLineSetting | undefined {
  return loadTuiConfig().statusLine;
}

function showCurrentConfig(ctx: CommandContext): void {
  const sl = getStatusLineConfig();
  if (!sl || sl.type !== "command" || !sl.command) {
    ctx.addItem(makeItem(ctx.sessionId, "info", "StatusLine — not configured", "m"));
    return;
  }
  const lines = [
    `command: '${sl.command}'`,
    `padding: ${sl.padding ?? 0}`,
  ];
  ctx.addItem(makeItem(ctx.sessionId, "info", `StatusLine\n  ${lines.join("\n  ")}`, "m"));
}

function stripOuterQuotes(s: string): string {
  const trimmed = s.trim();
  if (trimmed.length < 2) return trimmed;
  const first = trimmed[0];
  const last = trimmed[trimmed.length - 1];
  if ((first === "'" && last === "'") || (first === '"' && last === '"')) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function unescapeCommand(s: string): string {
  return s.replace(/\\(["\\])/g, "$1");
}

function setConfig(ctx: CommandContext, args: string): void {
  const command = unescapeCommand(stripOuterQuotes(args));
  if (!command) {
    ctx.addItem(makeItem(ctx.sessionId, "error", "usage: /statusline set <command>", "m"));
    return;
  }
  const existing = loadTuiConfig().statusLine;
  const padding = existing?.padding ?? 0;
  saveTuiConfig({ statusLine: { type: "command", command, padding } });
  ctx.restartStatusLine?.();
  ctx.addItem(
    makeItem(ctx.sessionId, "info", `StatusLine — Updated\n  command: '${command}'\n  padding: ${padding}`, "m"),
  );
}

function setPadding(ctx: CommandContext, args: string): void {
  const n = parseInt(args.trim(), 10);
  if (isNaN(n) || n < 0) {
    ctx.addItem(makeItem(ctx.sessionId, "error", "usage: /statusline padding <number> (0 or positive)", "m"));
    return;
  }
  const existing = loadTuiConfig().statusLine;
  if (!existing || existing.type !== "command" || !existing.command) {
    ctx.addItem(makeItem(ctx.sessionId, "error", "StatusLine not configured. Use /statusline set <command> first.", "m"));
    return;
  }
  saveTuiConfig({ statusLine: { ...existing, padding: n } });
  ctx.restartStatusLine?.();
  ctx.addItem(
    makeItem(ctx.sessionId, "info", `StatusLine — Updated\n  command: '${existing.command}'\n  padding: ${n}`, "m"),
  );
}

function clearConfig(ctx: CommandContext): void {
  saveTuiConfig({ statusLine: undefined });
  ctx.restartStatusLine?.();
  ctx.addItem(makeItem(ctx.sessionId, "info", "StatusLine — cleared", "m"));
}

function showHelp(ctx: CommandContext): void {
  const helpLines = [
    "StatusLine — runs a shell command every 2s and displays the output at the bottom of the screen. Supports multi-line output.",
    "",
    "How data is passed:",
    "  JSON is piped via stdin. On Windows, you can also use the file at $JIUWENSWARM_SL_FILE.",
    "",
    "Subcommands:",
    "  /statusline                 — show current configuration",
    "  /statusline get             — show current configuration",
    "  /statusline set <command>   — set the shell command to run",
    "  /statusline padding <n>     — set left & right padding (0 or positive)",
    "  /statusline clear           — remove statusline configuration",
    "  /statusline help            — show this guide",
    "  /statusline json            — show the real JSON data your command receives right now",
    "",
    "Agent-generated mode :",
    "  /statusline <prompt>        — describe what you want the statusline to show,",
    "                                and the agent will automatically generate the",
    "                                appropriate shell command and configure it for you.",
    "                                Example: /statusline show my shell PS1 configuration",
    "",
    "How to write a command:",
    "  Single field:  jq -r '.field'",
    "  Multiple fields: input=$(cat); echo \"$(echo \"$input\" | jq -r .field1) | $(echo \"$input\" | jq -r .field2)\"",
    "  Multi-line output: printf \"line1\\nline2\"",
    "",
    "Examples:",
    "  /statusline set 'jq -r \".mode + \" | \" + .model\"'",
    "  /statusline set 'input=$(cat); echo \"$(echo \"$input\" | jq -r .mode) | $(echo \"$input\" | jq -r .model)\"'",
    "  /statusline set 'basename \"$PWD\" && git branch --show-current 2>/dev/null || echo \"\"'",
    "  /statusline set 'printf \"%s\\n%s\" \"$(jq -r .mode)\" \"$(jq -r .cwd)\"'",
    "  /statusline set 'input=$(cat); pct=$(echo \"$input\" | jq -r \".context_window.remaining_percentage // empty\"); [ -n \"$pct\" ] && echo \"ctx: $pct% left\"'",
    "",
    "Tip: for long commands, save a script file and reference it in /statusline set.",
    "",
    "Available JSON fields:",
    "  session_id, session_name, cwd, mode, model, provider, version,",
    "  connection, theme, accent_color, transcript_mode, transcript_fold_mode,",
    "  is_processing, is_paused, is_interrupted, cancellable_work,",
    "  streaming_state, last_error, evolution_status,",
    "  active_subtask_count, todo_count, trusted_dirs,",
    "  usage.total_input_tokens, usage.total_output_tokens, usage.total_tokens,",
    "  context_window.context_window_size, .used_percentage, .remaining_percentage",
    "",
    "Use /statusline json to see the actual values right now.",
  ];
  ctx.addItem(makeItem(ctx.sessionId, "info", helpLines.join("\n"), "m"));
  showCurrentConfig(ctx);
}

function showActualJsonData(ctx: CommandContext): void {
  const data = ctx.getStatusLineJsonInput?.();
  if (!data) {
    ctx.addItem(makeItem(ctx.sessionId, "info", "StatusLine — JSON data not available", "m"));
    return;
  }
  ctx.addItem(
    makeItem(ctx.sessionId, "info", `StatusLine — current JSON input:\n${JSON.stringify(data, null, 2)}`, "m"),
  );
}

/**
 * Agent-generated mode: send a prompt to the agent to automatically write a statusline script.
 *
 * 
 * - User types: /statusline <description of what they want>
 * - TUI sends it as a chat message with skills_to_use=["script-creator"] metadata
 * - SkillUseRail loads script-creator SKILL.md
 * - Agent writes the script and configures the statusline
 */
function agentGenerate(ctx: CommandContext, prompt: string): void {
  if (!prompt.trim()) {
    // No prompt given — show current config
    showCurrentConfig(ctx);
    return;
  }

  // Send the prompt as a chat message. The backend build_user_prompt will
  // detect "/statusline" prefix and inject skills_to_use=["script-creator"].
  const fullPrompt = `/statusline ${prompt.trim()}`;
  const requestId = ctx.sendMessage(fullPrompt);
  if (!requestId) {
    ctx.addItem(
      makeItem(ctx.sessionId, "error", "offline: waiting for reconnect before sending statusline request"),
    );
  }
}

// Known subcommands that are handled locally (not sent to agent)
const KNOWN_SUBCOMMANDS = ["set", "padding", "clear", "help", "json", "get"];

export function createStatusLineCommand(): SlashCommand {
  return {
    name: "statusline",
    altNames: ["sl"],
    description: "Configure custom status line footer",
    usage: "/statusline <set|padding|clear|help|json> | /statusline <prompt>",
    example: "/statusline set 'echo $mode | $model'  OR  /statusline show my PS1 config",
    kind: CommandKind.BUILT_IN,
    takesArgs: true,
    subCommands: [
      {
        name: "set",
        description: "Set statusline command",
        usage: "/statusline set <command>",
        example: "/statusline set 'echo mode:$mode model:$model'",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: (ctx, args) => setConfig(ctx, args),
      },
      {
        name: "padding",
        description: "Set statusline padding (left & right spaces)",
        usage: "/statusline padding <number>",
        example: "/statusline padding 2",
        kind: CommandKind.BUILT_IN,
        takesArgs: true,
        action: (ctx, args) => setPadding(ctx, args),
      },
      {
        name: "clear",
        description: "Clear statusline configuration",
        usage: "/statusline clear",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        isSafeConcurrent: true,
        action: (ctx) => clearConfig(ctx),
      },
      {
        name: "help",
        description: "Show statusline usage guide",
        usage: "/statusline help",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        isSafeConcurrent: true,
        action: (ctx) => showHelp(ctx),
      },
      {
        name: "json",
        description: "Show the current JSON data your command would receive",
        usage: "/statusline json",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        isSafeConcurrent: true,
        action: (ctx) => showActualJsonData(ctx),
      },
      {
        name: "get",
        description: "Show current statusline configuration",
        usage: "/statusline get",
        kind: CommandKind.BUILT_IN,
        takesArgs: false,
        isSafeConcurrent: true,
        action: (ctx) => showCurrentConfig(ctx),
      },
    ],
    action: (ctx, args) => {
      const trimmedArgs = args.trim();
      if (!trimmedArgs) {
        // No args — show current config (per docs: "/statusline" shows current config)
        showCurrentConfig(ctx);
        return;
      }

      const firstWord = trimmedArgs.split(/\s+/)[0];

      // Check if first word is a known subcommand
      const matched = createStatusLineCommand().subCommands?.find((s) => s.name === firstWord);
      if (matched) {
        const rest = trimmedArgs.slice(firstWord.length).trim();
        matched.action(ctx, rest);
        return;
      }

      // NOT a known subcommand — treat as prompt for agent-generated mode
      agentGenerate(ctx, trimmedArgs);
    },
  };
}