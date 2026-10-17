import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const { default: React, act } = await import('react');
const { createRoot } = await import('react-dom/client');
const calls = [];
globalThis.rsiJudgeRequest = async (method, params) => {
  calls.push({ method, params });
  if (method === 'rsi.dataset.validate') return { valid: true, sample_count: 1, errors: [] };
  if (method === 'rsi.task.list') return { tasks: [] };
  return { task_id: 'probe', status: 'CREATED' };
};
const dialogCreateParams = [];
globalThis.rsiDialogCreateParams = dialogCreateParams;
const mocks = {
  '../rsiApi': `
    export const rsiDatasetValidate = async () => ({ valid: true, sample_count: 1, errors: [] });
    export const rsiTaskCreate = async params => {
      globalThis.rsiDialogCreateParams.push(params);
      return { task_id: 'probe', status: 'CREATED' };
    };
    export const rsiTaskList = async () => [];
    export const rsiTrainingStart = async () => ({ status: 'RUNNING' });
  `,
  '../../services/webClient': 'export const webRequest = (method, params) => globalThis.rsiJudgeRequest(method, params);',
  'react-i18next': "export const useTranslation = () => ({ t: key => key, i18n: { language: 'en' } });",
  '../../../stores/sessionStore': "export const useSessionStore = selector => selector({ availableModels: [{ model_name: 'test-model' }] });",
  '../../../components/ModelProviderIcon': 'export const ModelProviderIcon = () => null;',
  '../../../features/workspace/localFilePicker': "export const selectLocalFiles = async () => ({ ok: true, files: [{ path: '/fixtures/cases.json' }] });",
  '../../../features/workspace/projectDirectoryPicker': 'export const selectProjectDirectory = async () => ({ ok: false });',
};
await build({
  entryPoints: ['src/features/rsi/components/CreateExperimentDialog.tsx', 'src/features/rsi/rsiApi.ts'],
  outdir: 'node_modules/.cache/rsi-judge-selection', outbase: 'src/features/rsi', outExtension: { '.js': '.mjs' },
  bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic',
  define: { 'import.meta.env.DEV': 'false' },
  plugins: [{ name: 'test-boundaries', setup(builder) {
    builder.onResolve({ filter: /.*/ }, args => args.path in mocks ? { path: args.path, namespace: 'mock' } : undefined);
    builder.onLoad({ filter: /.*/, namespace: 'mock' }, args => ({ contents: mocks[args.path] }));
    builder.onResolve({ filter: /\.svg\?react$/ }, () => ({ path: 'icon', namespace: 'icon' }));
    builder.onLoad({ filter: /.*/, namespace: 'icon' }, () => ({ contents: 'export default () => null;' }));
  } }],
});
const { CreateExperimentDialog } = await import('../node_modules/.cache/rsi-judge-selection/components/CreateExperimentDialog.mjs');
const { rsiTaskCreate } = await import('../node_modules/.cache/rsi-judge-selection/rsiApi.mjs');

test('Agent selection reaches task.create with the current Harness default', async () => {
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(CreateExperimentDialog, {
      open: true, onClose() {}, onCreated() {},
    })));
    const selection = document.querySelector('[aria-label="rsi.createDialog.evaluationMethodLabel"]');
    assert.equal(selection.textContent.trim(), 'rsi.createDialog.evaluationMethodAgent');
    await act(async () => selection.click());
    const options = selection.parentElement.querySelectorAll('[role="menuitemradio"]');
    assert.equal(options[1].disabled, true);
    await act(async () => options[0].click());
    await act(async () => {
      const name = document.querySelector('[placeholder="rsi.createDialog.namePlaceholder"]');
      Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(name, 'Judge probe');
      name.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    });
    for (const trigger of document.querySelectorAll('.rsi-model-select > .rsi-model-select__trigger')) {
      await act(async () => trigger.click());
      await act(async () => document.querySelector('[role="menuitemradio"]').click());
    }
    await act(async () => document.querySelector('.rsi-create-dialog__path-btn').click());
    await act(async () => document.querySelector('.rsi-create-dialog__submit').click());
    assert.equal(
      document.querySelector('[aria-label="rsi.createDialog.pluginLabel"]'),
      null,
    );
    const request = dialogCreateParams.at(-1);
    assert.ok(request, 'form validation must complete before submitting');
    assert.equal(request.package_id, '');
    assert.equal(request.evaluation_method, 'llm_as_judge');
    assert.equal(request.input_file, '/fixtures/cases.json');
    assert.deepEqual(request.model_refs, { optimizer: 'test-model', tester: 'test-model' });
  } finally {
    await act(async () => root.unmount());
  }
});

test('script and legacy callers keep their grading route; artifact requests get no harness field', async () => {
  const base = { name: 'probe', scenario: 'HARNESS', input_file: '/fixtures/cases.json', model_refs: {} };
  for (const method of ['script_based', undefined]) {
    await rsiTaskCreate({ ...base, evaluation_method: method });
    assert.equal(calls.at(-1).params.evaluation_method, method);
  }
  await rsiTaskCreate({ ...base, scenario: 'ARTIFACT', artifact_type: 'PAPER', evaluation_method: 'llm_as_judge' });
  assert.equal(Object.hasOwn(calls.at(-1).params, 'evaluation_method'), false);
});

test.after(() => {
  dom.window.close();
  delete globalThis.rsiJudgeRequest;
  delete globalThis.rsiDialogCreateParams;
  delete globalThis.IS_REACT_ACT_ENVIRONMENT;
  delete globalThis.window;
  delete globalThis.document;
});
