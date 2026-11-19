import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

// Execute the production callbacks with injected dependencies, without mounting the full App.
export function loadCallback(path, name, bindings) {
  const source = ts.createSourceFile(
    path,
    readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8'),
    ts.ScriptTarget.Latest,
    true,
    path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  let callback;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === name) {
      const initializer = node.initializer;
      if (
        initializer && ts.isCallExpression(initializer) && initializer.expression.getText(source) === 'useCallback'
      ) {
        callback = initializer.arguments[0].getText(source);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(callback, `${path}: ${name} callback must exist`);
  const { outputText } = ts.transpileModule(`const callback = ${callback};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  });
  return new Function(...Object.keys(bindings), `${outputText}\nreturn callback;`)(...Object.values(bindings));
}
