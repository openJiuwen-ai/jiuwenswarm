import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function load(name, context, exports) {
  const source = fs.readFileSync(new URL(`../src/features/designer/${name}.ts`, import.meta.url), 'utf8');
  const tree = ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true);
  const script = tree.statements.filter((node) => !ts.isImportDeclaration(node))
    .map((node) => node.getText(tree).replace(/^export /, '')).join('\n');
  const { outputText } = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2020 } });
  return new Function('context', `with (context) { ${outputText}; return { ${exports} }; }`)(context);
}

test('an incomplete design model config keeps the landing draft and reports the problem', async () => {
  const opened = [];
  const context = {
    findDesignModelsProblem: async () => 'video-missing',
    toast: { open: (item) => opened.push(item) },
  };
  const { designLandingModelProblem } = load('designLandingDraft', context, 'designLandingModelProblem');
  assert.equal(await designLandingModelProblem(), 'video-missing');
  assert.deepEqual(opened, [{ content: 'video-missing', variant: 'error' }]);
});

test('a ready design model config does not report and lets the draft clear', async () => {
  const opened = [];
  const context = {
    findDesignModelsProblem: async () => null,
    toast: { open: (item) => opened.push(item) },
  };
  const { designLandingModelProblem } = load('designLandingDraft', context, 'designLandingModelProblem');
  assert.equal(await designLandingModelProblem(), null);
  assert.deepEqual(opened, []);
});

test('the landing composer clears only after the model check passes', () => {
  const source = fs.readFileSync(new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8');
  const gate = source.indexOf('await designLandingModelProblem()');
  const clear = source.indexOf("setInputValue(sid, '')");
  assert.ok(gate > 0 && clear > gate);
  assert.match(source.slice(gate, clear), /if \(modelProblem\) return/);
});
