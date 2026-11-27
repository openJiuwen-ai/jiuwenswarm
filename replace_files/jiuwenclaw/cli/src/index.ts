#!/usr/bin/env node

import { ProcessTerminal, TUI } from "@mariozechner/pi-tui";
import { parseArgs } from "node:util";
import { CliPiAppState } from "./app-state.js";
import { CommandService } from "./core/commands/CommandService.js";
import { createBuiltinCommands } from "./core/commands/registry.js";
import { WsClient } from "./core/ws-client.js";
import { AppScreen } from "./ui/app-screen.js";

const { values } = parseArgs({
  options: {
    url: { type: "string", default: "ws://127.0.0.1:19001/tui" },
    session: { type: "string" },
    token: { type: "string", default: "" },
    help: { type: "boolean", short: "h" },
  },
  strict: true,
});

if (values.help) {
  console.log(`jiuwenclaw-cli - Terminal CLI for JiuwenClaw

Options:
  --url <url>       Gateway CLI WebSocket URL (default: ws://127.0.0.1:19001/tui)
  --session <id>    Resume a specific session
  --token <token>   Authentication token
  -h, --help        Show this help
`);
  process.exit(0);
}

if (!process.stdin.isTTY || !process.stdout.isTTY) {
  console.error("jiuwenclaw-cli requires an interactive TTY");
  process.exit(1);
}

const wsClient = new WsClient(values.url ?? "ws://127.0.0.1:19001/tui", values.token ?? "");
const appState = new CliPiAppState(wsClient, values.session);
const commandService = new CommandService();
commandService.register(createBuiltinCommands());

const terminal = new ProcessTerminal();
const tui = new TUI(terminal);
tui.setClearOnShrink(true);

let closed = false;
let screen: AppScreen | null = null;

async function cancelBeforeExit(): Promise<void> {
  if (appState.getSnapshot().connectionStatus !== "connected") {
    return;
  }
  // 关 TUI 即请求服务端终止当前会话工作；idle 时一般为 no-op。
  appState.cancel({ showNotice: false });
  await new Promise((resolve) => setTimeout(resolve, 200));
}

async function closeUi(exitCode = 0): Promise<void> {
  if (closed) return;
  closed = true;
  try {
    await cancelBeforeExit();
  } catch {
    // Best effort only.
  }
  screen?.dispose();
  appState.stop();
  try {
    tui.stop();
  } catch {
    // Ignore repeated stop failures.
  }
  process.exit(exitCode);
}

async function crash(error: unknown): Promise<void> {
  const message = error instanceof Error ? (error.stack ?? error.message) : String(error);
  if (!closed) {
    try {
      await cancelBeforeExit();
    } catch {
      // Best effort only.
    }
    screen?.dispose();
    appState.stop();
    try {
      tui.stop();
    } catch {
      // Ignore repeated stop failures.
    }
    closed = true;
  }
  console.error(message);
  process.exit(1);
}

screen = new AppScreen(tui, appState, commandService, () => {
  void closeUi(0);
});
tui.addChild(screen);
tui.setFocus(screen);

process.on("SIGTERM", () => {
  void closeUi(0);
});
process.on("uncaughtException", (error) => {
  void crash(error);
});
process.on("unhandledRejection", (error) => {
  void crash(error);
});

appState.start();
tui.start();
