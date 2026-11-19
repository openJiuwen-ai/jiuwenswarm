import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const outfile = join(root, 'node_modules/.cache/history-artifact-reader/reader.mjs');
await build({
  stdin: {
    contents: `export { readArtifactWindow } from "./src/features/tool-events/historyArtifactReader.ts";
      export { normalizeToolResultPayload } from "./src/features/tool-events/toolEventNormalizer.ts";
      export { shouldProcessHistoryPayload, parseHistoryJsonFileToTimelinePreview } from "./src/features/historyRestore.ts";`,
    resolveDir: root, loader: 'ts',
  },
  outfile, bundle: true, packages: 'external', platform: 'node', format: 'esm',
  define: { 'import.meta.env': '{}' },
});
globalThis.window = globalThis;
globalThis.window.location = { protocol: 'http:', host: 'localhost:18180' };
globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
const { readArtifactWindow, normalizeToolResultPayload, shouldProcessHistoryPayload, parseHistoryJsonFileToTimelinePreview }
  = await import(pathToFileURL(outfile).href);
const reference = { sha256: 'a'.repeat(64), media_type: 'text/plain', bytes: 0 };

class Client {
  listeners = new Set();
  requests = [];
  constructor(text) { this.raw = Buffer.from(text); }
  on(_name, callback) { this.listeners.add(callback); return () => this.listeners.delete(callback); }
  emit(payload) { for (const callback of [...this.listeners]) callback({ payload }); }
  async request(method, params, options) {
    this.requests.push({ method, params, options });
    const offset = Number(params.cursor.split(':').at(-1));
    let end = Math.min(offset + 8192, this.raw.length);
    let content;
    while (end >= offset) {
      try { content = new TextDecoder('utf-8', { fatal: true }).decode(this.raw.subarray(offset, end)); break; }
      catch { end -= 1; }
    }
    queueMicrotask(() => {
      const message = { event_type: 'history.artifact', ...reference, bytes: this.raw.length,
        content, offset, next_offset: end, eof: end === this.raw.length };
      this.emit({ session_id: 'unrelated', cursor: params.cursor, message });
      this.emit({ session_id: params.session_id, cursor: params.cursor, message });
    });
    return { accepted: true };
  }
}

test('reads the complete UTF-8 original in bounded windows and releases subscriptions', async () => {
  const original = 'ORIGINAL_HEAD ' + '中文🙂"\n'.repeat(20000) + ' ORIGINAL_TAIL';
  const client = new Client(original);
  const ref = { ...reference, bytes: client.raw.length };
  const parts = [];
  let offset = 0;
  while (true) {
    const page = await readArtifactWindow(client, 'session-A', ref, offset);
    assert.ok(Buffer.byteLength(page.content) <= 65536);
    assert.equal(client.listeners.size, 0);
    parts.push(page.content);
    if (page.eof) break;
    assert.ok(page.nextOffset > offset);
    offset = page.nextOffset;
  }
  assert.equal(parts.join(''), original);
  assert.ok(client.requests.length > 8);
  assert.ok(client.requests.every(item => item.method === 'history.get' && item.params.limit === 1 && item.options.isStream));
});

test('artifact responses cannot finalize an ordinary page or cursor history load', () => {
  const payload = { session_id: 'session-A', cursor: `history-artifact:v1:${reference.sha256}:0`,
    page_idx: 1, status: 'done' };
  assert.equal(shouldProcessHistoryPayload(payload, 'session-A', 1), false);
  assert.equal(shouldProcessHistoryPayload(payload, 'session-A', undefined, false, undefined, null), false);
});

test('cancelled views stop reading and leave no subscriptions', async () => {
  const client = new Client('original');
  client.request = async () => ({ accepted: true });
  const controller = new AbortController();
  const pending = readArtifactWindow(client, 'session-A', { ...reference, bytes: 8 }, 0, controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  assert.equal(client.listeners.size, 0);
});

test('malformed ranges fail instead of displaying a successful partial original', async () => {
  const client = new Client('original');
  client.request = async (_method, params) => {
    queueMicrotask(() => client.emit({ session_id: 'session-A', cursor: params.cursor, message: {
      event_type: 'history.artifact', ...reference, bytes: 8, content: 'short', offset: 0, next_offset: 8, eof: true,
    } }));
  };
  await assert.rejects(readArtifactWindow(client, 'session-A', { ...reference, bytes: 8 }), /Invalid artifact range/);
  assert.equal(client.listeners.size, 0);
});

test('live and restored tool results retain references in the correct session without host paths', () => {
  const refs = { rendered_result: { ...reference, bytes: 100000, path: '/host/private/original' } };
  const payload = { tool_name: 'read_file', tool_call_id: 'tool-1', rendered_result: 'preview', payload_refs: refs };
  const live = normalizeToolResultPayload({ tool_result: payload, session_id: 'old-parent' }, 'current-fork');
  assert.equal(live.artifactSessionId, 'current-fork');
  assert.equal(live.payloadRefs.rendered_result.path, undefined);
  const restored = parseHistoryJsonFileToTimelinePreview([
    { role: 'user', content: 'read file', timestamp: 1794400000 },
    { role: 'assistant', event_type: 'chat.tool_call', request_id: 'q', timestamp: 1794400001,
      tool_call: { name: 'read_file', id: 'tool-1', arguments: {} } },
    { role: 'assistant', event_type: 'chat.tool_result', request_id: 'q', timestamp: 1794400002,
      event_payload: { tool_result: payload }, session_id: 'old-parent' },
  ], 'current-fork');
  const result = restored.executions[0].result;
  assert.equal(result.artifactSessionId, 'current-fork');
  assert.deepEqual(result.payloadRefs, live.payloadRefs);
});
