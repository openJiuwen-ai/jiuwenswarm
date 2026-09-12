import assert from 'node:assert/strict';
import test from 'node:test';

import {
  crossSessionAssistantMessageId,
  crossSessionUserMessageId,
  extractCrossSessionMessage,
} from '../node_modules/.cache/cross-session-message/utils/crossSessionMessage.js';
import { parseHistoryJsonFileToPreviewMessages } from '../node_modules/.cache/cross-session-message/features/historyRestore.js';

const crossSessionRecord = {
  message_origin: 'cross_session_agent',
  session_message_id: 'sm-1',
  cross_session: {
    message_id: 'sm-1',
    source_session_id: 'source-1',
    source_title: 'Source',
    content: 'external question',
  },
};

test('cross-session messages have stable user and assistant identities', () => {
  const metadata = extractCrossSessionMessage(crossSessionRecord);

  assert.deepEqual(metadata, {
    messageId: 'sm-1',
    sourceSessionId: 'source-1',
    sourceTitle: 'Source',
    content: 'external question',
  });
  assert.equal(crossSessionUserMessageId(metadata.messageId), 'cross-session-user-sm-1');
  assert.equal(
    crossSessionAssistantMessageId('execution-1', metadata.messageId),
    'cross-session-assistant-execution-1'
  );
});

test('history restore keeps prior conversation and marks the appended external turn', () => {
  const messages = parseHistoryJsonFileToPreviewMessages(
    [
      {
        role: 'user',
        content: 'original question',
        timestamp: 1,
      },
      {
        role: 'assistant',
        event_type: 'chat.final',
        content: 'original answer',
        timestamp: 2,
      },
      {
        role: 'user',
        content: 'external question',
        timestamp: 3,
        ...crossSessionRecord,
      },
      {
        role: 'assistant',
        event_type: 'chat.final',
        content: 'external answer',
        timestamp: 4,
        request_id: 'execution-1',
        ...crossSessionRecord,
      },
    ],
    'target-1'
  );

  assert.deepEqual(
    messages.map((message) => [message.role, message.content]),
    [
      ['user', 'original question'],
      ['assistant', 'original answer'],
      ['user', 'external question'],
      ['assistant', 'external answer'],
    ]
  );
  assert.deepEqual(messages[2].crossSession, {
    messageId: 'sm-1',
    sourceSessionId: 'source-1',
    sourceTitle: 'Source',
    content: 'external question',
  });
  assert.equal(messages[2].id, 'cross-session-user-sm-1');
  assert.equal(messages[3].id, 'cross-session-assistant-execution-1');
  assert.equal(messages[3].crossSession?.messageId, 'sm-1');
});
