import { matchesKey } from "@mariozechner/pi-tui";

import type { KeybindingAction } from "../core/keybindings/actions.js";
import { resolveAction } from "../core/keybindings/resolver.js";

/**
 * 快捷键约定（Ctrl+C）：
 * 第一次按下：设置本地中断标志（用于中断长运行命令如日志流）；
 * 如果有服务端任务运行，同时发送 chat.interrupt；
 * 如果处于空闲状态且无本地中断发生，则清空输入框；
 * 1 秒内再次按下则退出 CLI/TUI。
 */

let lastInterruptTime = 0;

export interface AppScreenKeymapDelegate {
  /** Interrupt server-side task (send chat.interrupt) */
  interruptTask(): void;
  /**
   * Set local interrupt flag only (for long-running local commands like log streaming).
   * Returns true if an active command WS request was cancelled — this means the
   * Ctrl+C keystroke was consumed by the command cancellation and the "double-press-
   * to-exit" timer should be reset.
   */
  requestLocalInterrupt(): boolean;
  /** Show a brief hint that pressing the given key again will exit */
  showExitHint(key: string): void;
  exitApp(): void;
  toggleTodos(): void;
  toggleTeamPanel(): void;
  toggleTranscript(): void;
  redraw(): void;
  clearInput(): void;
  isIdle(): boolean;
  /** Check if there's a server task running (for deciding whether to send chat.interrupt) */
  hasServerTask(): boolean;
}

interface KeyBindingDisplay {
  /** Default key id (display only — effective key may differ via keybindings.json). */
  key: Parameters<typeof matchesKey>[1];
  label: string;
  description: string;
  /** Resolver action; reserved keys (ctrl+c/d) have no rebindable action. */
  action?: KeybindingAction;
}

function runCtrlC(delegate: AppScreenKeymapDelegate): void {
  const now = Date.now();
  if (now - lastInterruptTime < 3000) {
    delegate.exitApp();
    return;
  }

  // Always set local interrupt flag (for long-running local commands).
  // Returns true if an active command request was cancelled — this means
  // Ctrl+C was consumed by command cancellation, not a generic interrupt.
  const commandCancelled = delegate.requestLocalInterrupt();

  // Only send chat.interrupt if there's a server task running
  if (delegate.hasServerTask()) {
    delegate.interruptTask();
  }

  // When a command (e.g. /recap) was cancelled, reset the double-press timer
  // so the user needs TWO fresh Ctrl+C presses to exit, not just one more.
  if (commandCancelled && !delegate.hasServerTask()) {
    lastInterruptTime = 0;
    return;
  }

  // If idle (no server task and no local command running), clear input
  if (delegate.isIdle()) {
    delegate.clearInput();
  }

  delegate.showExitHint("ctrl+c");
  lastInterruptTime = now;
}

function runCtrlD(delegate: AppScreenKeymapDelegate): void {
  const now = Date.now();
  if (now - lastInterruptTime < 3000) {
    delegate.exitApp();
    return;
  }
  lastInterruptTime = now;
  delegate.interruptTask();
  delegate.showExitHint("ctrl+d");
}

/**
 * Reserved (non-rebindable) keys with special double-press semantics.
 * These bypass the keybindings resolver entirely (see core/keybindings/reserved.ts).
 */
const RESERVED_BINDINGS: ReadonlyArray<{
  key: Parameters<typeof matchesKey>[1];
  run: (delegate: AppScreenKeymapDelegate) => void;
}> = [
  { key: "ctrl+c", run: runCtrlC },
  { key: "ctrl+d", run: runCtrlD },
];

/** Rebindable Global actions → handlers. */
const GLOBAL_ACTION_HANDLERS: Partial<Record<KeybindingAction, (d: AppScreenKeymapDelegate) => void>> = {
  "app:redraw": (d) => d.redraw(),
  "app:toggleTodos": (d) => d.toggleTodos(),
  "app:toggleTeamPanel": (d) => d.toggleTeamPanel(),
  "app:toggleTranscript": (d) => d.toggleTranscript(),
};

/**
 * Display metadata for the main-screen shortcuts (used by the shortcut hint
 * footer). Effective keys come from the resolver; these are the defaults.
 */
export const APP_SCREEN_KEY_BINDINGS: readonly KeyBindingDisplay[] = [
  { key: "ctrl+c", label: "ctrl+c", description: "中断任务；连按两次退出" },
  { key: "ctrl+d", label: "ctrl+d", description: "中断任务；连按两次退出" },
  { key: "escape", label: "esc", description: "取消任务；空闲时连按两次清空输入框", action: "app:cancelWork" },
  { key: "ctrl+l", label: "ctrl+l", description: "重绘屏幕", action: "app:redraw" },
  { key: "ctrl+t", label: "ctrl+t", description: "显示/隐藏 Todos 面板", action: "app:toggleTodos" },
  { key: "ctrl+g", label: "ctrl+g", description: "显示/隐藏 Team 面板", action: "app:toggleTeamPanel" },
  { key: "ctrl+o", label: "ctrl+o", description: "切换 transcript 紧凑/详细视图", action: "app:toggleTranscript" },
] as const;

export function handleAppScreenKeyInput(data: string, delegate: AppScreenKeymapDelegate): boolean {
  // Reserved keys first — never rebindable.
  for (const binding of RESERVED_BINDINGS) {
    if (matchesKey(data, binding.key)) {
      binding.run(delegate);
      return true;
    }
  }

  // Rebindable Global actions via the keybindings resolver.
  const action = resolveAction("Global", data);
  if (action) {
    const handler = GLOBAL_ACTION_HANDLERS[action];
    if (handler) {
      handler(delegate);
      return true;
    }
  }

  return false;
}
