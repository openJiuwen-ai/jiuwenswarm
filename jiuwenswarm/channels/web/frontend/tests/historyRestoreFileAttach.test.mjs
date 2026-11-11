import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const frontendRoot = fileURLToPath(new URL('..', import.meta.url));
const outfile = join(frontendRoot, 'node_modules/.cache/history-restore-file-attach/restore.mjs');
await build({
  stdin: {
    contents: 'export { attachFilesToPreviousAssistant } from "./src/features/historyRestore.ts";',
    resolveDir: frontendRoot,
    loader: 'ts',
  },
  outfile,
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  define: { 'import.meta.env': '{}' },
});
after(() => {
  // 防止缓存产物阻塞后续构建目录清理。
});

const { attachFilesToPreviousAssistant } = await import(
  pathToFileURL(outfile).href
);

function makeFiles(name) {
  return [{ name, path: `C:/ws/${name}`, size: 3 }];
}

test('同轮存在助手消息时挂到最近一条助手消息', () => {
  const messages = [
    { id: 'u1', role: 'user', content: '第一轮', timestamp: '2026-11-11T10:00:00Z' },
    { id: 'a1', role: 'assistant', content: '第一轮回答', timestamp: '2026-11-11T10:00:01Z' },
    { id: 'u2', role: 'user', content: '第二轮', timestamp: '2026-11-11T10:00:02Z' },
    { id: 'a2', role: 'assistant', content: '我先发文件', timestamp: '2026-11-11T10:00:03Z' },
  ];
  const attached = attachFilesToPreviousAssistant(messages, makeFiles('a.txt'));
  assert.equal(attached, true);
  assert.equal(messages[3].fileItems?.length, 1);
  assert.equal(messages[1].fileItems, undefined);
});

test('回溯遇到 user 消息即停在轮次边界，不跨轮挂载（#6159）', () => {
  // “先调工具发文件、后写正文”的轮次：文件条目之前本轮还没有助手消息，
  // 旧逻辑会跨过本轮 user 消息把产物挂到上一轮末尾。
  const messages = [
    { id: 'u1', role: 'user', content: '第一轮', timestamp: '2026-11-11T10:00:00Z' },
    { id: 'a1', role: 'assistant', content: '第一轮回答', timestamp: '2026-11-11T10:00:01Z' },
    { id: 'u2', role: 'user', content: '第二轮', timestamp: '2026-11-11T10:00:02Z' },
  ];
  const attached = attachFilesToPreviousAssistant(messages, makeFiles('b.txt'));
  assert.equal(attached, false);
  assert.equal(messages[1].fileItems, undefined);
  assert.deepEqual(messages.map((m) => m.fileItems ?? null), [null, null, null]);
});

test('数组里只有 user 消息时返回 false', () => {
  const messages = [{ id: 'u1', role: 'user', content: '第一轮', timestamp: '2026-11-11T10:00:00Z' }];
  const attached = attachFilesToPreviousAssistant(messages, makeFiles('c.txt'));
  assert.equal(attached, false);
  assert.equal(messages[0].fileItems, undefined);
});

test('team leader 合成消息（system）视为可挂载的助手消息且不跨轮', () => {
  const messages = [
    { id: 'u1', role: 'user', content: '第一轮', timestamp: '2026-11-11T10:00:00Z' },
    { id: 'team-leader-1', role: 'system', content: '', timestamp: '2026-11-11T10:00:01Z' },
  ];
  const attached = attachFilesToPreviousAssistant(messages, makeFiles('d.txt'));
  assert.equal(attached, true);
  assert.equal(messages[1].fileItems?.length, 1);
});
