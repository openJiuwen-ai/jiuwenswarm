import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const root = new URL('..', import.meta.url);
const sourceUrl = new URL('src/components/ChatPanel/MessageItem.tsx', root);
const source = await readFile(sourceUrl, 'utf8');

function between(sourceText, startMarker, endMarker) {
  const start = sourceText.indexOf(startMarker);
  assert.notEqual(start, -1, `${startMarker} should exist`);
  const end = sourceText.indexOf(endMarker, start);
  assert.notEqual(end, -1, `${endMarker} should exist after ${startMarker}`);
  return sourceText.slice(start, end);
}

const teamLeaderRenderer = between(
  source,
  'function TeamLeaderPlainTextMessage',
  'export function ContextCompressionLines',
);

assert.match(
  teamLeaderRenderer,
  /<A2UIMessageContent\b/,
  'team leader messages shown to users must use the A2UI-aware renderer',
);
assert.doesNotMatch(
  teamLeaderRenderer,
  /<MarkdownMessageBody\b/,
  'team leader messages shown to users must not render A2UI JSON as plain markdown',
);
assert.match(
  teamLeaderRenderer,
  /messageId=\{messageId\}/,
  'team leader A2UI renderer should receive a stable message id',
);

const teamEventBranch = between(
  source,
  "content.startsWith('team.event:')",
  '// 检查是否为 team_leader 消息',
);

assert.match(
  teamEventBranch,
  /messageId=\{id\}/,
  'team.event user-facing messages should pass their message id to the team leader renderer',
);

const teamLeaderIdBranch = between(
  source,
  "const isTeamLeaderMsg = id && id.startsWith('team-leader-');",
  '// 用户/助手消息',
);

assert.match(
  teamLeaderIdBranch,
  /messageId=\{id\}/,
  'team-leader messages should pass their message id to the team leader renderer',
);
assert.match(
  teamLeaderIdBranch,
  /isStreaming=\{isStreaming\}/,
  'streaming team-leader messages should forward streaming state to the A2UI renderer',
);
