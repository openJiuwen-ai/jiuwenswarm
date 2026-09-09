import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

/**
 * Intercepts agent file-editing tool calls from `chat.tool_call` events and
 * applies them directly to the VS Code workspace. Tracks file snapshots before
 * edits so the checkpoint / rewind feature can restore them.
 *
 * Supported tools:
 *   - str_replace_editor (command = str_replace | create)
 *   - write_file
 *   - create_file
 */

const EDIT_TOOLS = new Set(['str_replace_editor', 'write_file', 'create_file', 'edit_file']);

/** Snapshot of a file before it was edited. `null` means the file did not exist. */
export type FileSnapshot = string | null;

/** Maps file path → snapshot content before first edit this turn. */
let currentTurnSnapshots = new Map<string, FileSnapshot>();
/** Promoted from currentTurnSnapshots on chat.final; used for rewind. */
let lastTurnSnapshots = new Map<string, FileSnapshot>();

/** Clear snapshots at the start of a new user turn. */
export function clearSnapshots(): void {
  currentTurnSnapshots.clear();
  lastTurnSnapshots.clear();
}

/** Promote current turn snapshots to last turn (called on chat.final). Returns true if there are files to rewind. */
export function promoteSnapshots(): boolean {
  if (currentTurnSnapshots.size === 0) return false;
  lastTurnSnapshots = new Map(currentTurnSnapshots);
  currentTurnSnapshots.clear();
  return true;
}

/** Get the last turn snapshots for rewind. */
export function getLastTurnSnapshots(): Map<string, FileSnapshot> {
  return new Map(lastTurnSnapshots);
}

/** Clear last turn snapshots after rewind. */
export function clearLastTurnSnapshots(): void {
  lastTurnSnapshots.clear();
}

/**
 * Snapshot a file before it is edited, if not already snapshotted this turn.
 */
export function ensureSnapshot(filePath: string): void {
  if (currentTurnSnapshots.has(filePath)) return;
  try {
    if (fs.existsSync(filePath)) {
      currentTurnSnapshots.set(filePath, fs.readFileSync(filePath, 'utf-8'));
    } else {
      currentTurnSnapshots.set(filePath, null);
    }
  } catch {
    currentTurnSnapshots.set(filePath, null);
  }
}

/**
 * Main entry point. Parses a `chat.tool_call` event payload and applies the edit.
 * Returns true if the event was handled.
 */
export async function handleToolCall(event: Record<string, unknown>): Promise<boolean> {
  const payload = (event.payload as Record<string, unknown>) || {};
  const toolName = payload.tool_name as string | undefined;
  if (!toolName || !EDIT_TOOLS.has(toolName)) return false;

  const args = parseArguments(payload);
  if (!args) {
    console.warn(`[JiuwenSwarm] DiffApplier: could not parse arguments for ${toolName}`);
    return false;
  }

  // Check approval setting
  const cfg = vscode.workspace.getConfiguration('jiuwenswarm');
  const requireApproval = cfg.get<boolean>('approveEdits', false);

  switch (toolName) {
    case 'str_replace_editor': {
      const command = (args.command as string) || 'str_replace';
      const filePath = args.path as string;
      if (!filePath) return false;

      if (command === 'str_replace') {
        const oldStr = args.old_str as string;
        const newStr = (args.new_str as string) || '';
        if (oldStr === undefined) return false;
        return applyStrReplace(filePath, oldStr, newStr, requireApproval);
      }
      if (command === 'create') {
        const content = (args.file_text as string) || (args.content as string) || '';
        return writeEntireFile(filePath, content, true, requireApproval);
      }
      return false;
    }
    case 'edit_file': {
      // ACP-style edit: file_path + old_string/new_string
      const filePath = (args.file_path as string) || (args.path as string);
      if (!filePath) return false;
      const oldStr = (args.old_string as string) || (args.old_str as string);
      const newStr = (args.new_string as string) || (args.new_str as string) || '';
      if (oldStr === undefined) return false;
      return applyStrReplace(filePath, oldStr, newStr, requireApproval);
    }
    case 'write_file':
    case 'create_file': {
      const filePath = args.path as string;
      const content = (args.content as string) || '';
      if (!filePath) return false;
      const isNew = toolName === 'create_file' || !fs.existsSync(filePath);
      return writeEntireFile(filePath, content, isNew, requireApproval);
    }
    default:
      return false;
  }
}

// ──────────────────────────────────────────
// Core operations
// ──────────────────────────────────────────

/**
 * Replace the entire content of the document at *uri* through the VS Code
 * workspace-edit pipeline, then save. The covered span is derived from the
 * document's line layout — start of line 0 through the end of the final line —
 * which always equals the full text. Returns whether the edit was applied.
 */
async function replaceWholeFile(uri: vscode.Uri, content: string): Promise<boolean> {
  const doc = await vscode.workspace.openTextDocument(uri);
  const finalLine = doc.lineAt(doc.lineCount - 1);
  const wholeFile = new vscode.Range(new vscode.Position(0, 0), finalLine.range.end);
  const change = new vscode.WorkspaceEdit();
  change.replace(uri, wholeFile, content);
  const applied = await vscode.workspace.applyEdit(change);
  if (applied) {
    await doc.save();
  }
  return applied;
}

async function applyStrReplace(
  filePath: string,
  oldStr: string,
  newStr: string,
  requireApproval: boolean,
): Promise<boolean> {
  if (!fs.existsSync(filePath)) {
    notify(`File not found: ${filePath}`, true);
    return false;
  }

  const original = fs.readFileSync(filePath, 'utf-8');
  const idx = original.indexOf(oldStr);
  if (idx < 0) {
    notify(`Could not locate the target text in ${path.basename(filePath)}`, true);
    return false;
  }

  if (requireApproval) {
    const action = await vscode.window.showInformationMessage(
      `JiuwenSwarm proposes an edit to ${path.basename(filePath)}`,
      'Approve',
      'Reject',
    );
    if (action !== 'Approve') {
      notify(`Edit rejected: ${path.basename(filePath)}`);
      return false;
    }
  }

  ensureSnapshot(filePath);
  const proposed = original.substring(0, idx) + newStr + original.substring(idx + oldStr.length);

  try {
    const success = await replaceWholeFile(vscode.Uri.file(filePath), proposed);
    if (success) {
      notify(`Applied edit to ${path.basename(filePath)}`);
    }
    return success;
  } catch (e) {
    notify(`Failed to apply edit: ${e}`, true);
    return false;
  }
}

async function writeEntireFile(
  filePath: string,
  content: string,
  isNew: boolean,
  requireApproval: boolean,
): Promise<boolean> {
  const label = isNew ? 'Create' : 'Overwrite';

  if (requireApproval) {
    const action = await vscode.window.showInformationMessage(
      `JiuwenSwarm proposes to ${label.toLowerCase()} ${path.basename(filePath)}`,
      'Approve',
      'Reject',
    );
    if (action !== 'Approve') {
      notify(`${label} rejected: ${path.basename(filePath)}`);
      return false;
    }
  }

  ensureSnapshot(filePath);

  try {
    const dir = path.dirname(filePath);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }

    const uri = vscode.Uri.file(filePath);
    if (isNew || !fs.existsSync(filePath)) {
      // Create the file first so the later content write targets an existing
      // document (WorkspaceEdit can only replace within an existing file).
      const create = new vscode.WorkspaceEdit();
      create.createFile(uri, { overwrite: false });
      await vscode.workspace.applyEdit(create);
    }

    const success = await replaceWholeFile(uri, content);
    if (success) {
      notify(`${label} applied: ${path.basename(filePath)}`);
    }
    return success;
  } catch (e) {
    notify(`Failed to ${label.toLowerCase()}: ${e}`, true);
    return false;
  }
}

// ──────────────────────────────────────────
// Rewind
// ──────────────────────────────────────────

export async function performRewind(snapshots: Map<string, FileSnapshot>): Promise<{ restored: number; failed: number }> {
  let restored = 0;
  let failed = 0;

  for (const [filePath, originalContent] of snapshots) {
    try {
      if (originalContent === null) {
        // File didn't exist before edit — delete it
        if (fs.existsSync(filePath)) {
          fs.unlinkSync(filePath);
        }
      } else {
        // Restore original content
        fs.writeFileSync(filePath, originalContent, 'utf-8');
      }
      restored++;
    } catch {
      failed++;
    }
  }

  return { restored, failed };
}

// ──────────────────────────────────────────
// Helpers
// ──────────────────────────────────────────

function parseArguments(payload: Record<string, unknown>): Record<string, unknown> | null {
  const toolCall = payload.tool_call as Record<string, unknown> | undefined;
  if (!toolCall) return null;
  const args = toolCall.arguments;
  if (args && typeof args === 'object') return args as Record<string, unknown>;
  if (typeof args === 'string') {
    try {
      return JSON.parse(args) as Record<string, unknown>;
    } catch {
      return null;
    }
  }
  return null;
}

function notify(message: string, error = false): void {
  if (error) {
    vscode.window.showErrorMessage(`JiuwenSwarm: ${message}`);
  } else {
    vscode.window.showInformationMessage(`JiuwenSwarm: ${message}`);
  }
}
