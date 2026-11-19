import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function load(name, exports) {
  const source = fs.readFileSync(new URL(`../src/features/designer/${name}.ts`, import.meta.url), 'utf8');
  const tree = ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true);
  const script = tree.statements.filter((node) => !ts.isImportDeclaration(node))
    .map((node) => node.getText(tree).replace(/^export /, '')).join('\n');
  const { outputText } = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2020 } });
  return new Function(`with ({}) { ${outputText}; return { ${exports} }; }`)();
}

const api = [
  'designSessionPendingRestore',
  'alignDesignModelGate',
  'shouldHoldDesignModelDefault',
  'getDesignModelGate',
  'publishDesignModelGate',
  'requestDesignModelRestoreRetry',
].join(', ');

function fresh() {
  return load('designSessionModelRestore', api);
}

test('only an existing design session waits for its saved model', () => {
  const { designSessionPendingRestore: pending } = fresh();
  assert.equal(pending('design-project', 'design_saved', 'new'), 'design_saved');
  assert.equal(pending('design-project', '  design_saved  ', 'new'), 'design_saved');
  assert.equal(pending('chat-session', 'design_saved', 'new'), null);
  assert.equal(pending('design-project', 'new', 'new'), null);
  assert.equal(pending('design-project', '', 'new'), null);
  assert.equal(pending('design-project', null, 'new'), null);
});

test('the gate holds the default name until the saved id is restored', () => {
  const {
    alignDesignModelGate: align,
    shouldHoldDesignModelDefault: hold,
  } = fresh();
  const loading = align(null, 'design_saved');
  assert.deepEqual(loading, { sessionId: 'design_saved', status: 'loading', attempt: 0 });
  assert.equal(hold(loading, 'design_saved'), true);
  assert.equal(hold(loading, 'new'), false);
  const ready = { sessionId: 'design_saved', status: 'ready', attempt: 0 };
  assert.equal(align(ready, 'design_saved'), ready);
  assert.equal(hold(ready, 'design_saved'), false);
  const failed = { sessionId: 'design_saved', status: 'failed', attempt: 1 };
  assert.equal(align(failed, 'design_saved'), failed);
  assert.equal(hold(failed, 'design_saved'), true);
  assert.deepEqual(align(ready, 'design_other'), {
    sessionId: 'design_other', status: 'loading', attempt: 0,
  });
  assert.equal(align(ready, null), null);
  assert.equal(hold(null, 'design_saved'), false);
});

test('retry reads the saved model again and does not restart a load already in flight', () => {
  const {
    getDesignModelGate: current,
    publishDesignModelGate: publish,
    requestDesignModelRestoreRetry: retry,
  } = fresh();
  publish({ sessionId: 'design_saved', status: 'failed', attempt: 0 });
  retry();
  assert.deepEqual(current(), { sessionId: 'design_saved', status: 'loading', attempt: 1 });
  retry();
  assert.equal(current().attempt, 1);
  publish(null);
  retry();
  assert.equal(current(), null);
});

test('opening a design session does not fill the default model while the saved id is unread', () => {
  const app = fs.readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const hold = app.indexOf('shouldHoldDesignModelDefault(getDesignModelGate(), sessionId)');
  const fill = app.indexOf('setSelectedModelName(sessionId, defaultModelName)');
  assert.ok(hold > 0 && fill > hold);
  const selector = fs.readFileSync(
    new URL('../src/features/designer/components/DesignerModelSelector.tsx', import.meta.url),
    'utf8',
  );
  const loading = selector.indexOf('designer-model-selector-session-loading');
  const picker = selector.indexOf('<ChatModelSelector');
  assert.ok(loading > 0 && picker > loading);
  const panel = fs.readFileSync(
    new URL('../src/features/designer/components/DesignerChatPanel.tsx', import.meta.url),
    'utf8',
  );
  const guard = panel.indexOf("gate.status !== 'ready'");
  const write = panel.indexOf('setChatModel(modelName, modelId)');
  assert.ok(guard > 0 && write > guard);
});
