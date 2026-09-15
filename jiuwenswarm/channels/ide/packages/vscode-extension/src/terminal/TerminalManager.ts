import * as vscode from 'vscode';

let activeTerminal: vscode.Terminal | undefined;

/**
 * Run a shell command in a named IDE terminal so the user can see live output.
 * Reuses a single terminal per session; creates it lazily on first command.
 */
export function runCommand(command: string, cwd?: string): void {
  if (!activeTerminal) {
    activeTerminal = vscode.window.createTerminal({
      name: 'JiuwenSwarm',
      cwd: cwd || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
    });
  }
  activeTerminal.show();
  activeTerminal.sendText(command);
}

/** Dispose the managed terminal, if any. */
export function disposeTerminal(): void {
  try {
    activeTerminal?.dispose();
  } catch {
    // ignore
  }
  activeTerminal = undefined;
}

/**
 * Extract a shell command from a bash or run_command tool-call payload.
 * Handles various payload shapes sent by different server versions.
 */
export function extractCommand(payload: Record<string, unknown>): string | undefined {
  const args =
    extractArguments(payload) ||
    (payload.arguments as Record<string, unknown>) ||
    payload;
  if (!args || typeof args !== 'object') return undefined;

  const command =
    (args.command as string) ||
    (args.cmd as string) ||
    (args.shell_cmd as string) ||
    (args.bash_command as string);

  return command ? String(command).trim() : undefined;
}

function extractArguments(payload: Record<string, unknown>): Record<string, unknown> | undefined {
  if (payload.tool_call && typeof payload.tool_call === 'object') {
    const tc = payload.tool_call as Record<string, unknown>;
    if (tc.arguments && typeof tc.arguments === 'object') {
      return tc.arguments as Record<string, unknown>;
    }
    return tc as Record<string, unknown>;
  }
  if (payload.tool_input && typeof payload.tool_input === 'object') {
    return payload.tool_input as Record<string, unknown>;
  }
  if (payload.input && typeof payload.input === 'object') {
    return payload.input as Record<string, unknown>;
  }
  return undefined;
}
