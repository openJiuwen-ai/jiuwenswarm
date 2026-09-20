import assert from 'node:assert/strict';
import test from 'node:test';

process.env.NODE_ENV = 'production';

const { createElement } = await import('react');
const { renderToStaticMarkup } = await import('react-dom/server');
const { createInstance } = await import('i18next');
const { I18nextProvider } = await import('react-i18next');
const { ToolGroupDisplay } = await import(
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
