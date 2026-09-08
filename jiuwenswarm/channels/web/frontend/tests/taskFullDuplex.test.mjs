import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const read = (path) => readFileSync(path, 'utf8');

test('task composer delegates only its empty idle action to application plugins', () => {
  const source = read('src/components/ChatPanel/InputArea.tsx');
  assert.match(source, /<ApplicationPluginTaskInputActions/);
  assert.match(source, /!hasTextDraft/);
  assert.match(source, /attachments\.length === 0/);
  assert.match(source, /fallback=\{\(/);
  assert.match(source, /data-testid="chat-panel-input-send"/);
});

test('video duplex contributes a task action backed by the headless existing workflow', () => {
  const entry = read('../../../extensions/video_duplex/frontend/index.tsx');
  const action = read('../../../extensions/video_duplex/frontend/TaskFullDuplexAction.tsx');
  const runtime = read('../../../extensions/video_duplex/frontend/TaskFullDuplexRuntime.tsx');
  const panel = read('../../../extensions/video_duplex/frontend/VideoLivePanel/index.tsx');
  assert.match(entry, /applicationPluginTaskInputAction = TaskFullDuplexAction/);
  assert.match(entry, /applicationPluginTaskRuntime = TaskFullDuplexRuntime/);
  assert.match(action, /startTaskFullDuplex/);
  assert.match(runtime, /<VideoLivePanel[\s\S]*headless/);
  assert.match(panel, /getDisplayMedia/);
  assert.match(panel, /pendingScreenAutostartRef/);
  assert.match(panel, /void startRealtime\(\)/);
});

test('task full-duplex runtime is mounted above welcome and conversation composer branches', () => {
  const chatPanel = read('src/components/ChatPanel/index.tsx');
  const runtimeMount = chatPanel.indexOf('<ApplicationPluginTaskRuntimes');
  const conversationBranch = chatPanel.indexOf('{hasConversation ? (');
  assert.ok(runtimeMount > 0);
  assert.ok(conversationBranch > runtimeMount);
});

test('task full-duplex Core Agent progress reuses the normal reasoning and tool timeline', () => {
  const types = read('src/applicationPlugins/types.ts');
  const chatPanel = read('src/components/ChatPanel/index.tsx');
  const runtime = read('../../../extensions/video_duplex/frontend/TaskFullDuplexRuntime.tsx');
  const panel = read('../../../extensions/video_duplex/frontend/VideoLivePanel/index.tsx');
  assert.match(types, /onToolCall/);
  assert.match(types, /onToolResult/);
  assert.match(types, /onReasoning/);
  assert.match(types, /onReasoningClose/);
  assert.match(chatPanel, /addToolCall\(sid, toolCall/);
  assert.match(chatPanel, /addToolResult\(sid, toolResult/);
  assert.match(chatPanel, /appendReasoning\(sid, content/);
  assert.match(chatPanel, /closeReasoning\(sid/);
  assert.doesNotMatch(runtime, /name: ["']jiuwen_core_agent["']/);
  assert.match(runtime, /entry\.stage === ["']reasoning["']/);
  assert.match(runtime, /entry\.stage === ["']tool_call["']/);
  assert.match(runtime, /entry\.stage === ["']tool_result["']/);
  assert.match(panel, /onCoreAgentProgress\?\.\(["']progress["'], payload\)/);
  assert.match(panel, /onCoreAgentProgress\?\.\(["']completed["'], payload\)/);
});

test('task full-duplex assistant output reuses the native streaming message lifecycle', () => {
  const types = read('src/applicationPlugins/types.ts');
  const chatPanel = read('src/components/ChatPanel/index.tsx');
  const runtime = read('../../../extensions/video_duplex/frontend/TaskFullDuplexRuntime.tsx');
  const panel = read('../../../extensions/video_duplex/frontend/VideoLivePanel/index.tsx');

  assert.match(types, /onAssistantStream/);
  assert.match(chatPanel, /startStreaming\(sid, messageId, messageId\)/);
  assert.match(chatPanel, /updateMessage\(sid, messageId/);
  assert.match(chatPanel, /stopStreaming\(sid, messageId\)/);
  assert.match(runtime, /onAssistantStream=\{\(update\)/);
  assert.match(panel, /headless && onAssistantStream && responseId/);
  assert.match(panel, /isToolTurnCurrent/);
  assert.match(panel, /onStaleToolResult/);
  assert.match(panel, /turnId: payload\.turn_id/);
  assert.match(panel, /if \(!headless\) \{\s*const item = \{ id: \+\+chatSequenceRef\.current/);
  assert.doesNotMatch(panel, /streamingAnswerRef/);
  assert.doesNotMatch(panel, /const \[answer, setAnswer\]/);
});

test('task Full-duplex feature flag is independent from the plugin enabled state', () => {
  const action = read('../../../extensions/video_duplex/frontend/TaskFullDuplexAction.tsx');
  const plugin = read('../../../extensions/video_duplex/extension.py');
  assert.match(action, /config\.task_full_duplex_enabled/);
  assert.doesNotMatch(action, /VIDEO_DUPLEX_ENABLED|video\.duplex\.settings\.get/);
  assert.match(plugin, /available_when_disabled=True/);
});

test('task Full-duplex creates a real session before starting and persists its timeline', () => {
  const types = read('src/applicationPlugins/types.ts');
  const app = read('src/App.tsx');
  const action = read('../../../extensions/video_duplex/frontend/TaskFullDuplexAction.tsx');
  const runtime = read('../../../extensions/video_duplex/frontend/TaskFullDuplexRuntime.tsx');
  const backend = read('../../../extensions/video_duplex/backend/video_live.py');

  assert.match(types, /ensureSession: \(initialTitle\?: string\) => Promise<string \| null>/);
  assert.match(app, /ensureApplicationPluginSession/);
  assert.match(action, /await ensureSession\(/);
  assert.match(action, /startTaskFullDuplex\(readySessionId\)/);
  assert.match(runtime, /video\.conversation\.append/);
  assert.match(runtime, /historyQueues/);
  assert.match(backend, /"video\.conversation\.append": _append_conversation/);
  assert.doesNotMatch(runtime, /chat\.send/);
});
