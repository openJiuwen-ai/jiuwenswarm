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
  '../../../features/workspace/projectDirectoryPicker': "export const selectProjectDirectory = async () => ({ ok: true, path: '/fixtures/artifact' });",
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

test('Agent selection reaches task.create from the Harness branch', async () => {
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(CreateExperimentDialog, {
      open: true, onClose() {}, onCreated() {},
    })));
    // 显式选择 Harness，验证新增产物分支不影响原有评测流程。
    await act(async () => {
      const harnessTab = [...document.querySelectorAll('.rsi-create-dialog__branch-btn')]
        .find((btn) => btn.textContent === 'rsi.createDialog.typeHarness');
      assert.ok(harnessTab, 'harness branch tab must render');
      harnessTab.click();
    });
    const selection = document.querySelector('[aria-label="rsi.createDialog.evaluationMethodLabel"]');
    // 10 侧 ui/Select 为原生 <select>：断言选中项 value，而非 trigger 文本（textContent 会拼接全部 option）。
    assert.equal(selection.value, 'agent');
    const options = selection.querySelectorAll('option');
    assert.equal(options[1].disabled, true);
    await act(async () => {
      Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, 'value').set.call(selection, 'agent');
      selection.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
    });
    await act(async () => {
      const name = document.querySelector('[placeholder="rsi.createDialog.namePlaceholder"]');
      Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(name, 'Judge probe');
      name.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    });
    // 模型选择同为原生 <select>：两个（优化/测试模型）按 aria-label 定位，走原生 change 事件。
    for (const select of document.querySelectorAll('[aria-label="rsi.createDialog.modelPlaceholder"]')) {
      await act(async () => {
        Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, 'value').set.call(select, 'test-model');
        select.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
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

for (const artifactType of ['PAPER', 'PROGRAM']) {
  test(`${artifactType} can be created from the restored artifact branch`, async () => {
    const root = createRoot(document.getElementById('root'));
    try {
      await act(async () => root.render(React.createElement(CreateExperimentDialog, {
        open: true, onClose() {}, onCreated() {},
      })));
      const chooseBranch = async (label) => {
        const button = [...document.querySelectorAll('.rsi-create-dialog__branch-btn')]
          .find((item) => item.textContent === label);
        assert.ok(button, `missing branch: ${label}`);
        await act(async () => button.click());
      };
      await chooseBranch('rsi.createDialog.typeArtifact');
      await chooseBranch(artifactType === 'PAPER' ? 'rsi.createDialog.subtypePaper' : 'rsi.createDialog.subtypeProgram');
      await act(async () => {
        const name = document.querySelector('[placeholder="rsi.createDialog.namePlaceholder"]');
        Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(name, 'Artifact probe');
        name.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
        const model = document.querySelector('[aria-label="rsi.createDialog.modelPlaceholder"]');
        Object.getOwnPropertyDescriptor(dom.window.HTMLSelectElement.prototype, 'value').set.call(model, 'test-model');
        model.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
      await act(async () => document.querySelector('.rsi-create-dialog__path-btn').click());
      const count = dialogCreateParams.length;
      await act(async () => document.querySelector('.rsi-create-dialog__submit').click());
      assert.equal(dialogCreateParams.length, count + 1);
      const request = dialogCreateParams.at(-1);
      assert.equal(request.scenario, 'ARTIFACT');
      assert.equal(request.artifact_type, artifactType);
      assert.equal(request.artifact_path, '/fixtures/artifact');
      assert.deepEqual(request.model_refs, { optimizer: 'test-model' });
      assert.equal(Object.hasOwn(request, 'evaluation_method'), false);
    } finally {
      await act(async () => root.unmount());
    }
  });
}

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
