import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

function load(name, context, exports) {
  const source = fs.readFileSync(new URL(`../src/features/designer/${name}.ts`, import.meta.url), 'utf8');
  const tree = ts.createSourceFile(name, source, ts.ScriptTarget.Latest, true);
  const script = tree.statements.filter(node => !ts.isImportDeclaration(node))
    .map(node => node.getText(tree).replace(/^export /, '')).join('\n');
  const { outputText } = ts.transpileModule(script, { compilerOptions: { target: ts.ScriptTarget.ES2020 } });
  return new Function('context', `with (context) { ${outputText}; return { ${exports} }; }`)(context);
}
function fixture(models, fetched = models) {
  const state = { chatAvailableModels: models, defaultModelName: 'test-model', runtimes: {},
    setAvailableModels(value) { this.chatAvailableModels = value; } };
  const calls = [];
  const context = {
    useSessionStore: { getState: () => state }, useChatStore: { getState: () => ({}) },
    resolveChatModelSelection: entries => entries[0], i18n: { t: key => key },
    webRequest: async method => { calls.push(method); return { models: fetched }; },
    generationProblemFromConfig: (_config, kind) => `${kind}-missing`,
    nodeGenerationKind: node => node.kind,
  };
  Object.assign(context, load('designChatModelState', context, 'designChatModelReady'));
  return { ...load('designModelsReadiness', context,
    'findDesignChatModelProblem, findDesignModelsProblem, findDesignRunProblem'), calls, state };
}
for (const provider of ['Anthropic', 'OpenAI']) {
  test(`${provider} configured chat model is accepted from memory`, async () => {
    const f = fixture([{ model_name: 'test-model', model_provider: provider }]);
    assert.equal(await f.findDesignChatModelProblem(), null);
    assert.deepEqual(f.calls, []);
  });
  test(`${provider} configured chat model is accepted after catalog refresh`, async () => {
    const f = fixture([], [{ model_name: 'test-model', model_provider: provider }]);
    assert.equal(await f.findDesignChatModelProblem(), null);
    assert.equal(f.state.chatAvailableModels[0].model_provider, provider);
  });
}
test('an empty placeholder still reports a missing chat model', async () => {
  assert.equal(await fixture([{ model_name: '' }]).findDesignChatModelProblem(), 'designer.models.chatMissing');
});
test('Anthropic does not bypass media readiness checks', async () => {
  const f = fixture([{ model_name: 'test-model', model_provider: 'Anthropic' }]);
  assert.equal(await f.findDesignModelsProblem(), 'video-missing image-missing');
  assert.equal(await f.findDesignRunProblem([{ kind: 'image' }]), 'image-missing');
  assert.equal(await f.findDesignRunProblem([]), null);
});

for (const action of ['advance', 'rerunNode', 'restart']) {
  test(`${action} dispatches a configured workflow and still blocks missing media`, async () => {
    const { create } = await import('zustand');
    const graph = { graph_id: 'graph', metadata: { model_name: 'anthropic-model' }, nodes: [{ id: 'image', kind: 'image' }], edges: [] };
    const calls = [];
    let problem = null;
    const context = {
      create, derivePrimaryAction: () => 'execute',
      findDesignRunProblem: async () => problem,
      useDesignerStore: { getState: () => ({ domainGraph: graph, flushSave: async () => {} }) },
      designerGraphClient: { startRun: async params => { calls.push(params); return { run: null }; } },
      designerRunFailureMessage: () => null,
      DESIGNER_LEADER_NODE_ID: 'leader', DESIGNER_RUN_STATUS_RUNNING: 'running',
    };
    const { useDesignerRunStore: store } = load('designerRunStore', context, 'useDesignerRunStore');
    await store.getState()[action](graph, 'image');
    assert.equal(calls.length, 1);
    assert.equal(store.getState().runError, null);
    problem = 'image-missing';
    await store.getState()[action](graph, 'image');
    assert.equal(calls.length, 1);
    assert.equal(store.getState().runError, 'image-missing');
  });
}
