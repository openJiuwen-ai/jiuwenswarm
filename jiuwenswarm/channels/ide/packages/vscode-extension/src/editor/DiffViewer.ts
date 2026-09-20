import * as vscode from 'vscode';
import * as fs from 'fs';

const DIFF_SCHEME = 'jiuwenswarm-diff';

/** Virtual document provider that serves proposed file content for diff views. */
class DiffDocumentProvider implements vscode.TextDocumentContentProvider {
  private contents = new Map<string, string>();
  private _onDidChange = new vscode.EventEmitter<vscode.Uri>();
  readonly onDidChange = this._onDidChange.event;

  provideTextDocumentContent(uri: vscode.Uri): string {
    return this.contents.get(uri.path) ?? '';
  }

  setContent(path: string, content: string): vscode.Uri {
    this.contents.set(path, content);
    const uri = vscode.Uri.from({ scheme: DIFF_SCHEME, path });
    this._onDidChange.fire(uri);
    return uri;
  }

  deleteContent(path: string): void {
    this.contents.delete(path);
  }
}

const provider = new DiffDocumentProvider();

/** Register the diff document provider. Call once during activation. */
export function registerDiffProvider(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.workspace.registerTextDocumentContentProvider(DIFF_SCHEME, provider),
  );
}

/**
 * Show a side-by-side diff for a proposed file edit and ask the user to accept or reject.
 *
 * @param originalPath Absolute path to the existing file on disk.
 * @param proposedContent The content the agent wants to write.
 * @param toolName e.g. "str_replace_editor" or "write_file".
 * @returns `true` if user accepted, `false` if rejected.
 */
export async function showDiffAndPrompt(
  originalPath: string,
  proposedContent: string,
  toolName: string,
): Promise<boolean> {
  const fileName = originalPath.replace(/\\/g, '/').split('/').pop() ?? 'file';
  const originalUri = vscode.Uri.file(originalPath);
  const proposedUri = provider.setContent(originalPath, proposedContent);

  const title = `JiuwenSwarm — ${toolName}: ${fileName}`;
  await vscode.commands.executeCommand('vscode.diff', originalUri, proposedUri, title, {
    preview: true,
  });

  const choice = await vscode.window.showInformationMessage(
    `JiuwenSwarm proposes changes to ${fileName}`,
    { modal: false },
    'Accept',
    'Reject',
  );

  // Clean up virtual document
  provider.deleteContent(originalPath);

  // Close the diff editor
  const diffDoc = await vscode.workspace.openTextDocument(proposedUri);
  await vscode.window.showTextDocument(diffDoc, { preview: true, preserveFocus: true });
  await vscode.commands.executeCommand('workbench.action.closeActiveEditor');

  return choice === 'Accept';
}

/** Read current file content or return empty string if file doesn't exist. */
export function readOriginalContent(filePath: string): string {
  try {
    return fs.existsSync(filePath) ? fs.readFileSync(filePath, 'utf-8') : '';
  } catch {
    return '';
  }
}

/**
 * Compute the proposed file content from a str_replace_editor / edit_file tool
 * call WITHOUT writing to disk. Returns `undefined` if old_str can't be found.
 */
export function computeProposedContent(args: {
  path?: string;
  file_path?: string;
  old_str?: string;
  old_string?: string;
  new_str?: string;
  new_string?: string;
  content?: string;
  command?: string;
}): string | undefined {
  const command = args.command ?? 'str_replace';
  const original = readOriginalContent(args.file_path || args.path || '');

  if (command === 'create' || !original) {
    return args.content ?? args.new_string ?? args.new_str ?? '';
  }

  if (command === 'str_replace') {
    const oldStr = args.old_str ?? args.old_string;
    if (!oldStr || !original.includes(oldStr)) {
      return undefined;
    }
    return original.replace(oldStr, args.new_string ?? args.new_str ?? '');
  }

  if (command === 'write_file') {
    return args.content ?? args.new_string ?? args.new_str ?? '';
  }

  return undefined;
}
