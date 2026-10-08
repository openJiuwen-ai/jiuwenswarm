import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DESIGNER_CHAT_MAX_GRAPHS,
  DESIGNER_CHAT_STORAGE_KEY,
  extractDesignerGraphPrompt,
  extractDesignerGraphReferences,
  hasDesignerUserPrompt,
  persistDesignerChat,
  readPersistedDesignerChat,
  resolveBoundDesignerMessages,
  sanitizeDesignerChatMessages,
} from '../node_modules/.cache/designer-chat-history/designerChatHistory.js';

function memoryStorage(initial = '') {
  const bag = { value: initial };
  return {
    getItem(key) {
      assert.equal(key, DESIGNER_CHAT_STORAGE_KEY);
      return bag.value || null;
    },
    setItem(key, value) {
      assert.equal(key, DESIGNER_CHAT_STORAGE_KEY);
      bag.value = String(value);
    },
  };
}

test('extractDesignerGraphPrompt prefers Brief config.prompt', () => {
  assert.equal(
    extractDesignerGraphPrompt({
      description: 'graph description fallback',
      nodes: [
        { id: 'n_character', type: 'image', label: 'Character', config: { role: 'character_design' } },
        {
          id: 'n_brief',
          type: 'text',
          label: 'Brief',
          config: { role: 'brief', prompt: '  火车进站，年轻人走下车  ' },
        },
      ],
    }),
    '火车进站，年轻人走下车',
  );
});

test('extractDesignerGraphPrompt falls back to graph.description', () => {
  assert.equal(
    extractDesignerGraphPrompt({
      description: '火车站晨间短片',
      nodes: [{ id: 'n_brief', type: 'text', label: 'Brief', config: { role: 'brief' } }],
    }),
    '火车站晨间短片',
  );
});

test('resolveBoundDesignerMessages keeps pending when stored history is empty', () => {
  const pending = [
    {
      id: 'u1',
      role: 'user',
      content: '新的创作意图',
      kind: 'user',
      createdAt: 1,
    },
  ];
  assert.deepEqual(
    resolveBoundDesignerMessages({ stored: [], pending }),
    pending,
  );
  assert.equal(hasDesignerUserPrompt(pending), true);
});

test('sanitizeDesignerChatMessages drops thinking bubbles', () => {
  const cleaned = sanitizeDesignerChatMessages([
    { id: 'u1', role: 'user', content: '提示词', kind: 'user', createdAt: 1 },
    { id: 't1', role: 'assistant', content: '思考中', kind: 'thinking', createdAt: 2 },
    { id: 'd1', role: 'assistant', content: '已搭建', kind: 'bootstrap_done', createdAt: 3 },
  ]);
  assert.deepEqual(
    cleaned.map((item) => item.kind),
    ['user', 'bootstrap_done'],
  );
});

test('extractDesignerGraphReferences reads metadata.user_references', () => {
  const refs = extractDesignerGraphReferences({
    metadata: {
      user_references: [
        {
          id: 'ref_01',
          kind: 'image',
          filename: 'hero.png',
          mime_type: 'image/png',
          path: 'D:/proj/.designer/refs/hero.png',
          uri: 'file:///D:/proj/.designer/refs/hero.png',
          role: 'reference',
          order: 1,
        },
      ],
    },
  });
  assert.equal(refs[0].filename, 'hero.png');
  assert.equal(refs[0].kind, 'image');
});

test('sanitizeDesignerChatMessages keeps user references without dropping the bubble', () => {
  const cleaned = sanitizeDesignerChatMessages([
    {
      id: 'u1',
      role: 'user',
      content: '按参考图做短片',
      kind: 'user',
      createdAt: 1,
      references: [{ kind: 'image', filename: 'hero.png', mime_type: 'image/png', path: 'D:/hero.png' }],
    },
  ]);
  assert.equal(cleaned[0].references?.[0].filename, 'hero.png');
});

test('persistDesignerChat restores the original prompt after a reload', () => {
  const storage = memoryStorage();
  persistDesignerChat(
    {
      graph_a: [
        { id: 'u1', role: 'user', content: '火车进站', kind: 'user', createdAt: 1 },
        { id: 'd1', role: 'assistant', content: '已为你搭建初始设计工作流。', kind: 'bootstrap_done', createdAt: 2 },
      ],
    },
    storage,
  );
  const restored = readPersistedDesignerChat(storage);
  assert.equal(restored.graph_a[0].content, '火车进站');
  assert.equal(restored.graph_a[1].kind, 'bootstrap_done');
});

test('persistDesignerChat skips preview graphs and keeps older histories', () => {
  const storage = memoryStorage();
  persistDesignerChat(
    {
      graph_old: [{ id: 'u1', role: 'user', content: '旧提示词', kind: 'user', createdAt: 1 }],
    },
    storage,
  );
  persistDesignerChat(
    {
      preview_bootstrap: [{ id: 'u2', role: 'user', content: '预览', kind: 'user', createdAt: 2 }],
      graph_new: [{ id: 'u3', role: 'user', content: '新提示词', kind: 'user', createdAt: 3 }],
    },
    storage,
  );
  const restored = readPersistedDesignerChat(storage);
  assert.equal(restored.preview_bootstrap, undefined);
  assert.equal(restored.graph_old[0].content, '旧提示词');
  assert.equal(restored.graph_new[0].content, '新提示词');
});

test('persistDesignerChat prunes to the most recent graphs', () => {
  const storage = memoryStorage();
  const byId = {};
  for (let i = 0; i < DESIGNER_CHAT_MAX_GRAPHS + 5; i += 1) {
    byId[`g${i}`] = [{ id: `u${i}`, role: 'user', content: `p${i}`, kind: 'user', createdAt: i }];
  }
  persistDesignerChat(byId, storage);
  const restored = readPersistedDesignerChat(storage);
  assert.equal(Object.keys(restored).length, DESIGNER_CHAT_MAX_GRAPHS);
  assert.equal(restored.g0, undefined);
  assert.equal(restored[`g${DESIGNER_CHAT_MAX_GRAPHS + 4}`][0].content, `p${DESIGNER_CHAT_MAX_GRAPHS + 4}`);
});
