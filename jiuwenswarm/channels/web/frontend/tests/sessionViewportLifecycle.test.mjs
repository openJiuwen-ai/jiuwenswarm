import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const frontendRoot = new URL('../', import.meta.url);
const readSource = (path) => readFile(new URL(path, frontendRoot), 'utf8');

test('chat panel uses the shared session cache and initializes restoration on a cold mount', async () => {
  const source = await readSource('src/components/ChatPanel/index.tsx');
  assert.match(source, /lastSessionIdRef = useRef<string \| null>\(null\)/);
  assert.match(source, /saveSessionScrollTop\(sessionId, el\.scrollTop, isScrollAtBottom\(el\)\)/);
  assert.match(source, /getSessionScrollRestore\(sessionId, el\.scrollHeight, el\.clientHeight\)/);
  assert.match(source, /savedViewport\.wasAtBottom \|\| isScrollAtBottom\(el\)/);
  assert.match(source, /rememberSessionScrollTop\(lastSessionIdRef\.current \?\? '', el\)/);
  assert.doesNotMatch(source, /sessionScrollTopMapRef/);
});

test('only interactive timelines persist measured viewport snapshots by session', async () => {
  const source = await readSource('src/components/ChatPanel/MessageList.tsx');
  assert.match(source, /persistViewport = !staticTimeline && Boolean\(sessionId\)/);
  assert.match(source, /getSessionTimelineViewport\(timelineScope\)/);
  assert.match(source, /saveSessionTimelineViewport\(timelineScope, state\)/);
  assert.match(source, /viewportByScopeRef\.current\.get\(timelineScope\)/);
});

test('both session deletion paths clear the shared viewport cache', async () => {
  const app = await readSource('src/App.tsx');
  const sidebar = await readSource('src/multi-session/sidebar/useSidebarMenu.tsx');
  assert.match(app, /deleteSessionViewport\(id\)/);
  assert.match(sidebar, /deleteSessionViewport\(sessionId\)/);
});
