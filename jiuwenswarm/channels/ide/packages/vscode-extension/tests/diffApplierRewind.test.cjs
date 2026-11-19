const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const ts = require('typescript');

const source = path.join(__dirname, '../src/editor/DiffApplier.ts');
let editorWrites = 0;
const vscode = {
  Uri: { file: (filePath) => ({ fsPath: filePath }) },
  Position: class { constructor(line, character) { Object.assign(this, { line, character }); } },
  Range: class { constructor(start, end) { Object.assign(this, { start, end }); } },
  WorkspaceEdit: class { replace(uri, range, content) { Object.assign(this, { uri, content }); } },
  workspace: {
    openTextDocument: async (uri) => {
      const lines = fs.readFileSync(uri.fsPath, 'utf8').split('\n');
      return {
        lineCount: lines.length,
        lineAt: (line) => ({ range: { end: { line, character: lines[line].length } } }),
        save: async () => true,
      };
    },
    applyEdit: async (edit) => {
      fs.writeFileSync(edit.uri.fsPath, edit.content, 'utf8');
      editorWrites++;
      return true;
    },
  },
};
const compiled = ts.transpileModule(fs.readFileSync(source, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
});
const production = { exports: {} };
new Function('require', 'module', 'exports', compiled.outputText)(
  (name) => name === 'vscode' ? vscode : require(name), production, production.exports,
);
const { performRewind } = production.exports;

for (const original of ['$$', '$&', "$`", "$'", '$VAR ${expr} $1 $variable', '$$ $& $` $\'']) {
  test(`inverse rewind restores literal replacement tokens: ${original}`, async () => {
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'rewind-'));
    const filePath = path.join(directory, 'source.txt');
    const edited = 'replacement';
    const beforeWrites = editorWrites;
    try {
      fs.writeFileSync(filePath, `prefix ${edited} suffix`, 'utf8');
      const result = await performRewind(new Map(), new Map([
        [filePath, [{ oldStr: original, newStr: edited }]],
      ]));
      assert.deepEqual(result, { restored: 1, failed: 0 });
      assert.equal(fs.readFileSync(filePath, 'utf8'), `prefix ${original} suffix`);
      assert.equal(editorWrites, beforeWrites + 1);
    } finally {
      if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
      fs.rmdirSync(directory);
    }
  });
}

test('inverse rewind applies multiple edits in reverse order and keeps failed files unchanged', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'rewind-'));
  const filePath = path.join(directory, 'source.txt');
  const missingMatch = path.join(directory, 'other.txt');
  try {
    fs.writeFileSync(filePath, 'prefix final suffix', 'utf8');
    fs.writeFileSync(missingMatch, 'unchanged', 'utf8');
    const result = await performRewind(new Map(), new Map([
      [filePath, [{ oldStr: '$&', newStr: 'middle' }, { oldStr: 'middle', newStr: 'final' }]],
      [missingMatch, [{ oldStr: 'old', newStr: 'missing' }]],
    ]));
    assert.deepEqual(result, { restored: 1, failed: 1 });
    assert.equal(fs.readFileSync(filePath, 'utf8'), 'prefix $& suffix');
    assert.equal(fs.readFileSync(missingMatch, 'utf8'), 'unchanged');
  } finally {
    for (const file of [filePath, missingMatch]) {
      if (fs.existsSync(file)) fs.unlinkSync(file);
    }
    fs.rmdirSync(directory);
  }
});
