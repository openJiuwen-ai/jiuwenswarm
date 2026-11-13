import { describe, expect, it } from 'vitest';

import {
  addUserMessage, applyEvent, emptyConversation, parsePrompt, promptAnswers, replayHistory,
  type Conversation, type Entry,
} from './conversationModel';
import type { JsonObject } from './gatewayClient';

function play(events: [string, JsonObject][], from: Conversation = emptyConversation): Conversation {
  return events.reduce((conversation, [event, payload]) => applyEvent(conversation, { event, payload }), from);
}

/** The transcript without the ids the model hands out. */
function shown(conversation: Conversation): Omit<Entry, 'id'>[] {
  return conversation.entries.map(({ id: _id, ...entry }) => entry);
}

const PERMISSION: JsonObject = {
  request_id: 'call_7', interrupt_id: 'int_7', source: 'permission_interrupt', kind: 'permission',
  questions: [{
    question: '工具 `session_send_message` 需要授权才能执行', header: '权限审批: session_send_message',
    multi_select: false, card_id: 'call_7', tool_payload: { target_session_id: 's9' },
    options: [
      { value: 'allow_once', label: '本次允许', description: '仅本次授权执行' },
      { label: '拒绝' },
    ],
  }],
};

describe('a live turn', () => {
  it('shows a deferred tool once, under its own name, not the dispatch that ran it', () => {
    const conversation = play([
      ['chat.tool_call', { tool_call: { id: 'c1', name: 'tool_call', arguments: { name: 'verify_slides', args: {} } } }],
      ['chat.tool_call', { tool_call: { id: 'c1:target', name: 'verify_slides', arguments: {} } }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'c1:target', status: 'success' } }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'c1', status: 'success' } }],
    ]);
    expect(shown(conversation)).toEqual([{ kind: 'tool', name: 'verify_slides', status: 'done' }]);
  });

  it('shows a reply once when the backend repeats its final around a pause for a task pane tool', () => {
    const events: [string, JsonObject][] = [
      ['chat.delta', { content: '我先读取整体信息。' }],
      ['chat.final', { content: '我先读取整体信息。' }],
      ['chat.tool_call', { tool_call: { id: 'c1', name: 'read_presentation_context' } }],
      ['chat.final', { content: '我先读取整体信息。' }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'c1', status: 'success' } }],
    ];
    const replies = (conversation: Conversation) => shown(conversation).filter(entry => entry.kind === 'assistant');
    expect(replies(play(events))).toEqual([{ kind: 'assistant', text: '我先读取整体信息。' }]);
    expect(replies(replayHistory(events.map(([event_type, payload]) => ({ event_type, ...payload })))))
      .toEqual([{ kind: 'assistant', text: '我先读取整体信息。' }]);
  });

  it('streams one reply until a tool call, then a new one', () => {
    const conversation = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.delta', { content: '先读' }],
      ['chat.delta', { content: '一下。' }],
      ['chat.tool_call', { tool_call: { id: 'c1', name: 'read_presentation_context' } }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'c1', status: 'success' } }],
      ['chat.tool_call', { tool_call: { id: 'c2', name: 'verify_slides' } }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'c2', status: 'error' } }],
      ['chat.delta', { content: '共 12 页' }],
      ['chat.final', { content: '共 12 页。' }],
      ['chat.processing_status', { is_processing: false }],
    ]);
    expect(shown(conversation)).toEqual([
      { kind: 'assistant', text: '先读一下。' },
      { kind: 'tool', name: 'read_presentation_context', status: 'done' },
      { kind: 'tool', name: 'verify_slides', status: 'failed' },
      { kind: 'assistant', text: '共 12 页。' },
    ]);
    expect(conversation.turn).toBe('idle');
  });

  it('keeps running while the turn pauses for the pane\'s tool calls, not when it pauses for anything else', () => {
    const paused = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.client_tool_request', { request_id: 'c1' }],
      ['chat.processing_status', { is_processing: false }],
    ]);
    expect(paused.turn).toBe('running');
    const resumedAndDone = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.processing_status', { is_processing: false }],
    ], paused);
    expect(resumedAndDone.turn).toBe('idle');
  });

  it('ends on an error or a cancel, and an error says why', () => {
    const failed = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.error', { error: '模型超时' }],
    ]);
    expect(failed.turn).toBe('idle');
    expect(shown(failed)).toEqual([{ kind: 'notice', tone: 'error', text: '出错了：模型超时' }]);
    const cancelled = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.ask_user_question', PERMISSION],
      ['chat.interrupt_result', { intent: 'cancel', success: true }],
    ]);
    expect(cancelled.turn).toBe('idle');
    expect(cancelled.prompt).toBeUndefined();
  });

  it('leaves no step running once the turn has ended, nor a task still sending', () => {
    const conversation = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.tool_call', { tool_call: { id: 'c1', name: 'list_slide_shapes' } }],
      ['chat.tool_call', { tool_call: { id: 'c9', name: 'session_send_message',
        arguments: { target_session_id: 's9', message: '同步' } } }],
      ['chat.error', { error: 'model overloaded' }],
    ]);
    expect(conversation.entries.flatMap(entry => (entry.kind === 'tool' || entry.kind === 'outgoing'
      ? [entry.status] : []))).toEqual(['failed', 'failed']);
  });

  it('a new message from the user drops the question the turn asked', () => {
    const asked = play([['chat.ask_user_question', PERMISSION]]);
    expect(asked.prompt?.requestId).toBe('call_7');
    expect(addUserMessage(asked, '算了').prompt).toBeUndefined();
  });
});

describe('questions a turn asks', () => {
  it('reads an approval card with its options, card and tool arguments', () => {
    expect(parsePrompt(PERMISSION)).toEqual({
      requestId: 'call_7', interruptId: 'int_7', source: 'permission_interrupt',
      questions: [{
        question: '工具 `session_send_message` 需要授权才能执行', header: '权限审批: session_send_message',
        multiSelect: false, cardId: 'call_7', toolArguments: { target_session_id: 's9' }, toolName: 'session_send_message',
        options: [
          { value: 'allow_once', label: '本次允许', description: '仅本次授权执行' },
          { label: '拒绝' },
        ],
      }],
    });
  });

  it('ignores a question this pane cannot answer', () => {
    expect(parsePrompt({ ...PERMISSION, source: 'evolution_interrupt' })).toBeUndefined();
    expect(play([['chat.ask_user_question', { ...PERMISSION, source: undefined }]]).prompt).toBeUndefined();
  });

  it('answers with the option value, else its label, naming the card as the web chat does', () => {
    const prompt = parsePrompt(PERMISSION)!;
    const [allow, reject] = prompt.questions[0].options;
    expect(promptAnswers(prompt, [{ selected: [allow] }])).toEqual([{
      question: '工具 `session_send_message` 需要授权才能执行', selected_options: ['allow_once'], card_id: 'call_7',
    }]);
    expect(promptAnswers(prompt, [{ selected: [reject] }])[0].selected_options).toEqual(['拒绝']);
  });

  it('answers the agent\'s own question with the options picked and the text typed', () => {
    const prompt = parsePrompt({
      request_id: 'ask_1', source: 'ask_user_interrupt',
      questions: [{ question: '封面风格？', options: [{ label: '简洁' }, { label: '图片' }], multi_select: true }],
    })!;
    const [plain, picture] = prompt.questions[0].options;
    expect(promptAnswers(prompt, [{ selected: [plain, picture], customInput: '偏蓝色' }])).toEqual([
      { question: '封面风格？', selected_options: ['简洁', '图片'], custom_input: '偏蓝色' },
    ]);
  });
});

describe('messages between documents', () => {
  const crossSession = {
    message_origin: 'cross_session_agent', session_message_id: 'm1',
    cross_session: { message_id: 'm1', source_session_id: 's9', source_title: '市场分析.pptx', content: '核对第 5 页' },
  };

  it('shows a task another document sent once, with the mailbox\'s status', () => {
    const conversation = play([
      ['session.message.updated', { message: {
        message_id: 'm1', source_title: '市场分析.pptx', content: '核对第 5 页', status: 'queued' } }],
      ['chat.processing_status', { is_processing: true, ...crossSession }],
      ['chat.final', { content: '一致。', ...crossSession }],
      ['session.message.updated', { message: { message_id: 'm1', status: 'succeeded' } }],
      ['chat.processing_status', { is_processing: false, ...crossSession }],
    ]);
    expect(shown(conversation)).toEqual([
      { kind: 'incoming', sourceTitle: '市场分析.pptx', text: '核对第 5 页', status: 'succeeded' },
      { kind: 'assistant', text: '一致。' },
    ]);
  });

  it('shows a task sent to another document until its send reports back', () => {
    const call = { tool_call: { id: 'c9', name: 'session_send_message', arguments: JSON.stringify(
      { target_session_id: 's9', message: '同步标题样式' }) } };
    const sending = play([['chat.tool_call', call]]);
    expect(shown(sending)).toEqual(
      [{ kind: 'outgoing', targetSessionId: 's9', text: '同步标题样式', status: 'sending' }]);
    const failed = play([['chat.tool_result', { tool_result: { tool_call_id: 'c9', success: false } }]], sending);
    expect(shown(failed)[0]).toMatchObject({ status: 'failed' });
  });

  it('shows a task sent once when the history and the live stream both carry its call', () => {
    const call = { tool_call: { id: 'c9', name: 'session_send_message', arguments: JSON.stringify(
      { target_session_id: 's9', message: '同步标题样式' }) } };
    expect(shown(play([['chat.tool_call', call], ['chat.tool_call', call]]))).toHaveLength(1);
  });
});

describe('a conversation\'s history', () => {
  it('shows what was said and done, and the questions asked only as notes', () => {
    const conversation = replayHistory([
      { role: 'user', content: '把标题改短', request_id: 'r1' },
      { role: 'assistant', event_type: 'chat.tool_call', request_id: 'r1',
        tool_call: { id: 'c1', name: 'list_slide_shapes' } },
      { role: 'assistant', event_type: 'chat.ask_user_question', request_id: 'r1', ...PERMISSION },
      { role: 'assistant', event_type: 'chat.ask_user_answer', request_id: 'r1',
        answers: [{ selected_options: ['本次允许'] }] },
      { role: 'assistant', event_type: 'chat.final', content: '改好了。', request_id: 'r1' },
      { role: 'assistant', event_type: 'chat.final', content: '另一轮。', request_id: 'r2' },
      { role: 'user', content: '核对第 5 页', message_origin: 'cross_session_agent', cross_session: {
        message_id: 'm1', source_session_id: 's9', source_title: '市场分析.pptx' } },
    ]);
    expect(shown(conversation)).toEqual([
      { kind: 'user', text: '把标题改短' },
      { kind: 'tool', name: 'list_slide_shapes', status: 'failed' },
      { kind: 'notice', tone: 'info', text: '已询问：工具 `session_send_message` 需要授权才能执行' },
      { kind: 'notice', tone: 'info', text: '已回答：本次允许' },
      { kind: 'assistant', text: '改好了。' },
      { kind: 'assistant', text: '另一轮。' },
      { kind: 'incoming', sourceTitle: '市场分析.pptx', text: '核对第 5 页', status: 'succeeded' },
    ]);
    expect(conversation.prompt).toBeUndefined();
    expect(conversation.turn).toBe('idle');
  });
});

describe('what a turn did', () => {
  const edit = (id: string, status = 'success'): [string, JsonObject][] => [
    ['chat.tool_call', { tool_call: { id, name: 'edit_slide_text', arguments: { ref: `${id}:1` } } }],
    ['chat.tool_result', { tool_result: { tool_call_id: id, status } }],
  ];

  it('keeps what the model said a call is for, and its arguments', () => {
    const conversation = play([['chat.tool_call', { tool_call: {
      id: 'c1', name: 'list_slide_shapes', arguments: '{"slide_id":"258"}', call_goal: '找标题' } }]]);
    expect(shown(conversation)).toEqual([{
      kind: 'tool', name: 'list_slide_shapes', status: 'running', goal: '找标题', args: { slide_id: '258' } }]);
  });

  it('closes a turn that changed the presentation with the calls its undo takes back, a failed edit too', () => {
    const conversation = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.tool_call', { tool_call: { id: 'r1', name: 'read_presentation_context' } }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'r1', status: 'success' } }],
      ...edit('e1'), ...edit('e2', 'error'),
      ['chat.final', { content: '改好了。' }],
      ['chat.processing_status', { is_processing: false }],
    ]);
    expect(shown(conversation).at(-1)).toEqual({ kind: 'changes', callIds: ['e1', 'e2'] });
  });

  it('adds nothing after a turn that only read, and waits out a question before closing', () => {
    const readOnly = play([
      ['chat.processing_status', { is_processing: true }],
      ['chat.tool_call', { tool_call: { id: 'r1', name: 'read_presentation_context' } }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'r1', status: 'success' } }],
      ['chat.processing_status', { is_processing: false }],
    ]);
    expect(readOnly.entries.some(entry => entry.kind === 'changes')).toBe(false);

    const asked = play([
      ['chat.processing_status', { is_processing: true }], ...edit('e1'),
      ['chat.ask_user_question', PERMISSION], ['chat.processing_status', { is_processing: false }],
    ]);
    expect(asked.entries.some(entry => entry.kind === 'changes')).toBe(false);
    const answered = play([
      ['chat.processing_status', { is_processing: true }], ...edit('e2'), ['chat.processing_status', { is_processing: false }],
    ], { ...asked, prompt: undefined });
    expect(shown(answered).at(-1)).toEqual({ kind: 'changes', callIds: ['e1', 'e2'] });
  });
});
