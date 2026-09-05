import * as vscode from 'vscode';

/**
 * Code Action provider: shows a lightbulb "Fix with JiuwenSwarm" on any line
 * that has an Error or Warning diagnostic.
 *
 * Parity with JetBrains' Alt+Enter intention action.
 */
export class FixWithAiCodeActionProvider implements vscode.CodeActionProvider {
  public static readonly providedCodeActionKinds = [
    vscode.CodeActionKind.QuickFix,
  ];

  provideCodeActions(
    document: vscode.TextDocument,
    _range: vscode.Range | vscode.Selection,
    context: vscode.CodeActionContext,
    _token: vscode.CancellationToken,
  ): vscode.CodeAction[] {
    const diagnostics = context.diagnostics.filter(
      (d) =>
        d.severity === vscode.DiagnosticSeverity.Error ||
        d.severity === vscode.DiagnosticSeverity.Warning,
    );
    if (diagnostics.length === 0) return [];

    const action = new vscode.CodeAction(
      'Fix with JiuwenSwarm',
      vscode.CodeActionKind.QuickFix,
    );
    action.command = {
      command: 'jiuwenswarm.fixDiagnostic',
      title: 'Fix with JiuwenSwarm',
      arguments: [document, diagnostics],
    };
    action.diagnostics = diagnostics;
    return [action];
  }
}

/**
 * Build a prefill message from diagnostics and surrounding code.
 * Mirrors the logic in JetBrains' FixWithAiIntention.kt.
 */
export function buildDiagnosticPrefill(
  document: vscode.TextDocument,
  diagnostics: vscode.Diagnostic[],
): string {
  // Pick the first diagnostic (the most relevant one)
  const primary = diagnostics[0];
  const line = primary.range.start.line;
  const errorText = primary.message.replace(/\s+/g, ' ').trim();

  // Grab ±7 lines of context
  const startLine = Math.max(0, line - 7);
  const endLine = Math.min(document.lineCount - 1, line + 7);
  const startOffset = document.lineAt(startLine).range.start;
  const endOffset = document.lineAt(endLine).range.end;
  const codeSnippet = document.getText(new vscode.Range(startOffset, endOffset));

  const lang = document.languageId;
  const fileName = document.fileName.split(/[\\/]/).pop() || 'file';

  return `Fix this error in ${fileName}:\n\n` +
    `Error:\n${errorText}\n\n` +
    `\`\`\`${lang}\n${codeSnippet.trimEnd()}\n\`\`\`\n\n`;
}
