import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

process.env.NODE_ENV = 'production';

const { createElement } = await import('react');
const { renderToStaticMarkup } = await import('react-dom/server');
const { createInstance } = await import('i18next');
const { I18nextProvider } = await import('react-i18next');
await build({
  stdin: {
    contents: `export { ToolGroupDisplay } from './src/components/ChatPanel/ToolGroupDisplay';
      export { TimelineRowStateProvider } from './src/components/ChatPanel/timelineRowState';`,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/tool-group-display/ToolGroupDisplay.mjs',
  define: { 'import.meta.env.DEV': 'false' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [
    {
      name: 'ui-test-svg-components',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
      },
    },
  ],
});

const { ToolGroupDisplay, TimelineRowStateProvider } = await import(
  '../node_modules/.cache/tool-group-display/ToolGroupDisplay.mjs'
);

async function renderToolGroup() {
  const i18n = createInstance();
  await i18n.init({
    lng: 'zh',
    fallbackLng: 'zh',
    initImmediate: false,
    resources: {
      zh: {
        translation: {
          chatUi: {
            toolResult: {
              flowchart: '流程图',
            },
          },
        },
      },
    },
  });
  const beamSearch = {
    language: 'cn',
    roundIndex: 1,
    graph: {
      nodes: [{ id: 'writer', label: '写作技能', status: 'seed' }],
      edges: [],
    },
  };
  const execution = {
    toolCallId: 'compose-1',
    toolCall: {
      id: 'compose-1',
      name: 'symphony_compose_graph',
      arguments: { query: '生成报告' },
    },
    result: {
      toolName: 'symphony_compose_graph',
      result: '编排完成',
      success: true,
      beamSearch,
      mermaid: 'flowchart LR\nwriter("写作技能")',
    },
    status: 'completed',
    startedAt: '2026-09-01T00:00:00Z',
    updatedAt: '2026-09-01T00:00:01Z',
    timeoutAt: '2026-09-01T00:01:00Z',
  };

  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(ToolGroupDisplay, {
        executions: [execution],
        showAvatar: false,
      }),
    ),
  );
}

test('renders the Beam search tree before the flowchart tool result', async () => {
  const html = await renderToolGroup();
  const beamIndex = html.indexOf('data-testid="chat-panel-beam-search-tree"');
  const toolTreeIndex = html.indexOf('data-testid="chat-panel-tool-tree"');
  const flowchartIndex = html.indexOf('data-testid="chat-panel-tool-result-mermaid"');

  assert.notEqual(beamIndex, -1);
  assert.notEqual(toolTreeIndex, -1);
  assert.notEqual(flowchartIndex, -1);
  assert.ok(beamIndex < toolTreeIndex);
  assert.ok(toolTreeIndex < flowchartIndex);
  assert.match(html, />流程图</);
  assert.doesNotMatch(html, /专家团/);
});

async function renderExpandedResult({ name = 'ask_user', args, result, success = true, pending = false }) {
  const execution = {
    toolCallId: 'redaction-test',
    toolCall: { id: 'redaction-test', name, arguments: args },
    result: { toolName: name, result, success, pending },
    status: pending ? 'pending' : success ? 'completed' : 'error',
  };
  const before = JSON.stringify(execution);
  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: {}, initImmediate: false });
  const html = renderToStaticMarkup(createElement(I18nextProvider, { i18n },
    createElement(TimelineRowStateProvider, {
      prefix: 'test', values: new Map([['test/tool-open-keys', { 'redaction-test': true }]]),
    }, createElement(ToolGroupDisplay, { executions: [execution], showAvatar: false }))));
  assert.equal(JSON.stringify(execution), before, 'display must not mutate execution data');
  assert.match(html, /chat-panel-tool-execution-details-result/);
  return html;
}

for (const args of [
  { query: '请输入连接器令牌' },
  { query: 'Credentials', questions: [{ question: 'Enter API KEY' }] },
  { query: 'Choose', questions: [{ question: '更新密码方式', options: [{ label: '自己登录' }] }] },
]) {
  test(`sensitive ask_user result is not exposed in expanded details: ${JSON.stringify(args)}`, async () => {
    const html = await renderExpandedResult({ args, result: "{'answer': 'INVALID_TEST_SECRET_4470'}" });
    assert.doesNotMatch(html, /INVALID_TEST_SECRET_4470/);
    assert.match(html, /chatUi.toolResult.answersInSummary/);
  });
}

for (const scenario of [
  { args: { query: '选择颜色' }, result: '蓝色' },
  { args: { query: '选择颜色', questions: [{ question: '颜色' }] }, result: '蓝色' },
  { name: 'bash', args: { query: 'token' }, result: 'unchanged command output' },
  { args: { query: 'password' }, result: 'Tool unavailable', success: false },
  { args: { query: 'password' }, result: 'Waiting for input', pending: true },
]) {
  test(`unrelated tool results and diagnostics remain visible: ${scenario.result}`, async () => {
    const html = await renderExpandedResult(scenario);
    assert.ok(html.includes(scenario.result));
    assert.doesNotMatch(html, /chatUi.toolResult.answersInSummary/);
  });
}

test('answer text resembling a failure must not bypass display masking', async () => {
  const html = await renderExpandedResult({ args: { query: 'API key' }, result: 'success=False TEST_SECRET' });
  assert.doesNotMatch(html, /TEST_SECRET/);
});
