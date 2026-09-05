import * as vscode from 'vscode';
import { execSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

export interface ContextMetrics {
  byteLength: number;
  estimatedTokens: number;
  diagnosticsCount: number;
  otherOpenFilesCount: number;
  projectTreeFilesCount: number;
}

export interface CollectedContext {
  text?: string;
  metrics: ContextMetrics;
}

/**
 * Collects IDE context (active file path, language, selection, diagnostics,
 * other open files, project tree, and git status) and formats it as a structured
 * text block to prepend to outgoing chat messages.
 *
 * @param mentionedPaths - explicit file paths the user @-mentioned in their message
 */
export function collectContext(mentionedPaths: string[] = []): CollectedContext {
  const editor = vscode.window.activeTextEditor;

  const filePath = editor?.document.fileName;
  const lang = editor?.document.languageId;

  const selection = editor && !editor.selection.isEmpty
    ? editor.document.getText(editor.selection)
    : null;

  const diagnostics = editor ? collectDiagnostics(editor.document) : [];

  // Other open files (excluding the active one)
  const otherOpenFiles = filePath ? collectOtherOpenFiles(filePath) : [];

  // Project tree (2-level directory listing, respects settings)
  const cfg = vscode.workspace.getConfiguration('jiuwenswarm');
  const projectTreeEnabled = cfg.get<boolean>('projectTree.enabled', true);
  const projectTreeMaxFiles = cfg.get<number>('projectTree.maxFiles', 200);
  const projectTreeResult = projectTreeEnabled ? collectProjectTree(projectTreeMaxFiles) : undefined;
  const projectTree = projectTreeResult?.text;

  // Git context
  const gitInfo = filePath ? collectGitContext(filePath) : undefined;

  // Project rules (.jiuwenswarm/instructions.md, .jiuwenswarm/rules.md, or AGENTS.md)
  const projectRules = collectProjectRules();

  // Mentioned files (@-mentions from the user's message)
  const mentionedFilesText = mentionedPaths.length > 0 ? collectMentionedFiles(mentionedPaths) : undefined;

  // Nothing meaningful if everything is empty
  const emptyMetrics = {
    byteLength: 0,
    estimatedTokens: 0,
    diagnosticsCount: diagnostics.length,
    otherOpenFilesCount: otherOpenFiles.length,
    projectTreeFilesCount: projectTreeResult?.fileCount ?? 0,
  };

  if (!filePath && !selection && diagnostics.length === 0 && otherOpenFiles.length === 0 && !projectTree && !gitInfo && !projectRules && !mentionedFilesText) {
    return { metrics: emptyMetrics };
  }

  const lines: string[] = [];
  lines.push('<!-- IDE Context -->');
  if (filePath) {
    lines.push(`Active file: ${filePath}  (${lang})`);
    lines.push(`Cursor line: ${editor!.selection.active.line + 1}`);
  }
  if (selection && selection.trim()) {
    lines.push('');
    lines.push('Selected code:');
    lines.push('```');
    lines.push(selection.trimEnd());
    lines.push('```');
  }
  if (diagnostics.length > 0) {
    lines.push('');
    lines.push(`Diagnostics (${diagnostics.length}):`);
    diagnostics.forEach((d) => lines.push(`  \u2022 ${d}`));
  }
  if (otherOpenFiles.length > 0) {
    lines.push('');
    lines.push(`Other open files (${otherOpenFiles.length}):`);
    otherOpenFiles.forEach((f) => lines.push(`  ${f}`));
  }
  if (projectTree) {
    lines.push('');
    lines.push('Project structure:');
    lines.push(projectTree);
  }
  if (gitInfo) {
    lines.push('');
    lines.push(gitInfo);
  }
  if (projectRules) {
    lines.push('');
    lines.push('Project rules:');
    lines.push(projectRules);
  }
  if (mentionedFilesText) {
    lines.push('');
    lines.push(mentionedFilesText);
  }
  lines.push('<!-- End IDE Context -->');
  const text = lines.join('\n');
  return {
    text,
    metrics: {
      ...emptyMetrics,
      byteLength: Buffer.byteLength(text, 'utf8'),
      estimatedTokens: estimateTokens(text),
    },
  };
}

function collectDiagnostics(doc: vscode.TextDocument): string[] {
  const diags = vscode.languages.getDiagnostics(doc.uri);
  const result: string[] = [];
  for (const d of diags) {
    // Only warnings and errors (not info/hint)
    if (d.severity === vscode.DiagnosticSeverity.Error || d.severity === vscode.DiagnosticSeverity.Warning) {
      const line = d.range.start.line + 1;
      const msg = d.message.replace(/\s+/g, ' ').trim();
      result.push(`Line ${line}: ${msg}`);
    }
    if (result.length >= 10) break;
  }
  return result;
}

function collectOtherOpenFiles(activePath: string): string[] {
  const files: string[] = [];
  for (const tab of vscode.window.tabGroups.all.flatMap((g) => g.tabs)) {
    if (tab.input instanceof vscode.TabInputText) {
      const uri = tab.input.uri.fsPath;
      if (uri && uri !== activePath) {
        files.push(uri);
      }
    }
  }
  return files.slice(0, 10);
}

const SKIP_DIRS = new Set([
  '.git', '.gradle', '.idea', 'build', 'dist', 'node_modules',
  'target', '__pycache__', '.venv', 'venv', '.tox', 'coverage', '.cache',
]);

function collectProjectTree(maxFiles = 200): { text: string; fileCount: number } | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) return undefined;
  const root = folders[0].uri.fsPath;
  try {
    const counter = { count: 0 };
    const tree = buildDirTree(root, 0, maxFiles, counter);
    return tree ? { text: tree, fileCount: counter.count } : undefined;
  } catch {
    return undefined;
  }
}

function buildDirTree(
  dir: string,
  depth: number,
  maxFiles: number,
  counter: { count: number },
): string | undefined {
  if (counter.count >= maxFiles) return undefined;
  const entries = fs.readdirSync(dir, { withFileTypes: true })
    .filter((e) => !SKIP_DIRS.has(e.name) && !e.name.startsWith('.'))
    .sort((a, b) => {
      if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1;
      return a.name.localeCompare(b.name);
    });

  if (entries.length === 0) return undefined;

  const lines: string[] = [];
  for (const e of entries) {
    if (counter.count >= maxFiles) {
      lines.push(`${'  '.repeat(depth)}… (truncated at ${maxFiles} files)`);
      break;
    }
    const indent = '  '.repeat(depth);
    if (e.isDirectory()) {
      lines.push(`${indent}${e.name}/`);
      if (depth < 1) {
        const sub = buildDirTree(path.join(dir, e.name), depth + 1, maxFiles, counter);
        if (sub) lines.push(sub);
      }
    } else {
      lines.push(`${indent}${e.name}`);
      counter.count++;
    }
  }
  return lines.join('\n');
}

function collectGitContext(filePath: string): string | undefined {
  try {
    const workDir = path.dirname(filePath);
    const branch = execSync('git rev-parse --abbrev-ref HEAD', {
      cwd: workDir,
      encoding: 'utf-8',
      timeout: 5000,
    }).trim();
    if (!branch) return undefined;

    const status = execSync('git status --porcelain', {
      cwd: workDir,
      encoding: 'utf-8',
      timeout: 5000,
    }).trim();
    const statusLines = status.split('\n').filter((l) => l.trim().length > 0);

    let result = `Git: branch=${branch}`;
    if (statusLines.length === 1) {
      result += ', 1 uncommitted change';
    } else if (statusLines.length > 1) {
      result += `, ${statusLines.length} uncommitted changes`;
    } else {
      result += ', clean';
    }
    return result;
  } catch {
    return undefined;
  }
}

/** Reads the first project-rules file found at the workspace root. */
function collectProjectRules(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) return undefined;
  const root = folders[0].uri.fsPath;
  const candidates = [
    path.join(root, '.jiuwenswarm', 'instructions.md'),
    path.join(root, '.jiuwenswarm', 'rules.md'),
    path.join(root, 'AGENTS.md'),
  ];
  for (const candidate of candidates) {
    try {
      if (fs.existsSync(candidate)) {
        const content = fs.readFileSync(candidate, 'utf-8').trim();
        if (content) return content;
      }
    } catch {
      // skip unreadable files
    }
  }
  return undefined;
}

/** Reads each @-mentioned file and returns their contents as a formatted block. */
function collectMentionedFiles(paths: string[]): string | undefined {
  const parts: string[] = [];
  for (const p of paths) {
    try {
      if (fs.existsSync(p)) {
        const content = fs.readFileSync(p, 'utf-8');
        const ext = path.extname(p).replace('.', '') || 'text';
        parts.push(`@${p}:`);
        parts.push('```' + ext);
        parts.push(content.trimEnd());
        parts.push('```');
      }
    } catch {
      // skip unreadable files
    }
  }
  return parts.length > 0 ? parts.join('\n') : undefined;
}

/** Returns all files visible in the workspace for @-mention autocomplete. */
export function gatherWorkspaceFiles(): Array<{ name: string; path: string; rel: string }> {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || folders.length === 0) return [];
  const root = folders[0].uri.fsPath;
  const results: Array<{ name: string; path: string; rel: string }> = [];
  const counter = { count: 0 };
  collectFiles(root, root, results, counter, 500);
  return results;
}

function collectFiles(
  dir: string,
  root: string,
  out: Array<{ name: string; path: string; rel: string }>,
  counter: { count: number },
  limit: number,
): void {
  if (counter.count >= limit) return;
  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const e of entries) {
    if (counter.count >= limit) break;
    if (SKIP_DIRS.has(e.name) || e.name.startsWith('.')) continue;
    const full = path.join(dir, e.name);
    if (e.isDirectory()) {
      collectFiles(full, root, out, counter, limit);
    } else {
      out.push({ name: e.name, path: full, rel: path.relative(root, full) });
      counter.count++;
    }
  }
}

export function estimateTokens(text: string): number {
  const length = text.trim().length;
  return length > 0 ? Math.ceil(length / 4) : 0;
}
