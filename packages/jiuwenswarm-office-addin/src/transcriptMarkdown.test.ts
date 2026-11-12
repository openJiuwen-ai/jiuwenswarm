import { describe, expect, it } from 'vitest';

import { applyEvent, emptyConversation } from './conversationModel';
import type { GatewayEvent, JsonObject } from './gatewayClient';
import { transcriptFileName, transcriptMarkdown } from './transcriptMarkdown';

const events: [string, JsonObject][] = [
  ['chat.processing_status', { is_processing: true }],
  ['chat.tool_call', { tool_call: { id: 'r1', name: 'read_presentation_context' } }],
  ['chat.tool_result', { tool_result: { tool_call_id: 'r1', status: 'success' } }],
  ['chat.tool_call', { tool_call: { id: 'w1', name: 'presentation_apply_patch' } }],
  ['chat.tool_result', { tool_result: { tool_call_id: 'w1', status: 'success' } }],
  ['chat.final', { content: '标题改好了。' }],
  ['chat.processing_status', { is_processing: false }],
];

describe('the transcript file', () => {
  it('says who said what, with the steps between by name', () => {
    const conversation = events.map(([event, payload]): GatewayEvent => ({ event, payload }))
      .reduce(applyEvent, { ...emptyConversation, entries: [{ kind: 'user', id: 'u1', text: '把标题改成蓝色' }] });
    expect(transcriptMarkdown('季度汇报.pptx', conversation.entries, new Map())).toBe([
      '# 季度汇报.pptx', '**你**', '把标题改成蓝色', '> 过程：读取演示文稿、更新页面', '**WorkSwarm**', '标题改好了。',
    ].join('\n\n') + '\n');
  });

  it('is named after the document and the day', () => {
    expect(transcriptFileName('季度汇报.pptx', new Date(2026, 10, 11))).toBe('季度汇报 对话记录 2026-11-11.md');
    expect(transcriptFileName('', new Date(2026, 0, 2))).toBe('新对话 对话记录 2026-01-02.md');
  });
});
