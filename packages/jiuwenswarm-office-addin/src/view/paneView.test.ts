// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { SessionId } from '../clientAttachment';
import { applyEvent, emptyConversation, type Conversation } from '../conversationModel';
import { documentCopy } from '../copy';
import type { GatewayEvent, JsonObject } from '../gatewayClient';
import type { PaneState } from '../paneController';
import { POWERPOINT } from '../powerPointApp';
import { ENTER_GUARD_MS } from './interaction';
import { mountPane } from './paneView';

const S1 = 's1' as SessionId;
const ATTACHED = {
  kind: 'attached', sessionId: S1, attachmentId: 'a1', displayName: '季度汇报.pptx', renewEverySeconds: 30,
} as const;
const PERMISSION: JsonObject = {
  request_id: 'call_7', interrupt_id: 'int_7', source: 'permission_interrupt',
  questions: [{
    question: '工具 `session_send_message` 需要授权才能执行', header: '权限审批: session_send_message',
    tool_payload: { target_session_id: 's9', message: '同步标题样式' },
    options: [
      { value: 'allow_once', label: '本次允许' }, { value: 'session_allow', label: '会话内记住' },
      { value: 'always_allow', label: '始终允许' }, { value: 'reject', label: '拒绝' },
    ],
  }],
};

function conversationOf(...events: [string, JsonObject][]): Conversation {
  return events.map(([event, payload]): GatewayEvent => ({ event, payload }))
    .reduce((conversation, event) => applyEvent(conversation, event, POWERPOINT.contract), emptyConversation);
}

let ticks: (() => void)[];
let clockNow: number;
/** Answering the prompt takes the sheet's keyboard listener off the document before the next test. */
let unmount: (() => void) | undefined;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date', 'performance'] });
  ticks = [];
  clockNow = Date.now();
});
afterEach(() => {
  unmount?.();
  vi.useRealTimers();
});

/** The pane on a controller whose state the test sets and whose actions it records. */
function mounted(initial: Partial<PaneState> = {}) {
  let state: PaneState = {
    attachment: ATTACHED, conversation: emptyConversation, conversations: [], documents: new Map([['s9', '市场分析.pptx']]),
    loadingHistory: false, connection: { kind: 'open' }, models: [], undo: { offers: new Set() }, ...initial,
  };
  const listeners = new Set<(state: PaneState) => void>();
  const controller = {
    state: () => state,
    subscribe: (listener: (state: PaneState) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    send: vi.fn(async (_text: string) => true),
    stop: vi.fn(async () => undefined),
    answer: vi.fn(async (): Promise<void> => undefined),
    switchTo: vi.fn(async () => undefined),
    newConversation: vi.fn(async () => undefined),
    continueHere: vi.fn(async () => undefined),
    chooseModel: vi.fn(),
    dismissAlert: vi.fn(),
    undo: vi.fn(async (_card: string, _overwrite?: boolean) => undefined),
    cancelUndo: vi.fn(),
  };
  const root = document.createElement('div');
  document.body.replaceChildren(root);
  mountPane(root, controller, documentCopy.presentation,
    { now: () => clockNow, every: (_ms, tick) => ticks.push(tick) });
  const set = (change: Partial<PaneState>): void => {
    state = { ...state, ...change };
    listeners.forEach(listener => listener(state));
  };
  unmount = () => set({ conversation: emptyConversation });
  const buttons = (): HTMLButtonElement[] => [...root.querySelectorAll('button')];
  const button = (name: string): HTMLButtonElement => {
    const found = buttons().find(item => (item.getAttribute('aria-label') ?? item.textContent) === name);
    if (!found) throw new Error(`no button ${name}; there are ${buttons().map(item => item.textContent).join(' | ')}`);
    return found;
  };
  const input = (): HTMLTextAreaElement => root.querySelector('.composer-input')!;
  const text = (selector: string): string => root.querySelector(selector)?.textContent ?? '';
  const announced = (): string => root.querySelector('[aria-live]')?.textContent ?? '';
  const second = (): void => {
    clockNow += 1000;
    vi.advanceTimersByTime(1000);
    ticks.forEach(tick => tick());
  };
  return { root, controller, set, button, buttons, input, text, announced, second };
}

const key = (name: string, target: EventTarget = document.body): void => {
  target.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true }));
};

describe('the approval sheet', () => {
  it('says what the request would do and to which document, and offers the three choices', () => {
    const { text, buttons, input } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    expect(text('.approval-ask')).toBe('允许发送任务给另一份文档？');
    expect(text('.approval-payload')).toContain('目标文档市场分析.pptx');
    expect(text('.approval-payload')).toContain('内容同步标题样式');
    expect(buttons().filter(item => item.closest('.sheet-actions')).map(item => item.textContent))
      .toEqual(['拒绝', '本对话内始终允许', '允许一次']);
    expect(input().disabled).toBe(true);
  });

  it('answers with the clicked option, value and all', () => {
    const { controller, button } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    button('本对话内始终允许').click();
    expect(controller.answer).toHaveBeenCalledWith([{ selected: [{ value: 'session_allow', label: '会话内记住' }] }]);
  });

  it('rejects on Esc', () => {
    const { controller } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    key('Escape');
    expect(controller.answer).toHaveBeenCalledWith([{ selected: [{ value: 'reject', label: '拒绝' }] }]);
  });

  it('takes an Enter as consent only once the sheet has been up for a moment', () => {
    const { controller } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    vi.advanceTimersByTime(ENTER_GUARD_MS - 1);
    key('Enter');
    expect(controller.answer).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    key('Enter');
    expect(controller.answer).toHaveBeenCalledWith([{ selected: [{ value: 'allow_once', label: '本次允许' }] }]);
  });

  it('takes no Enter as consent right after other keys, as when the user was typing', () => {
    const { controller } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    vi.advanceTimersByTime(1000);
    key('a');
    key('b');
    vi.advanceTimersByTime(100);
    key('Enter');
    expect(controller.answer).not.toHaveBeenCalled();
    vi.advanceTimersByTime(ENTER_GUARD_MS);
    key('Enter');
    expect(controller.answer).toHaveBeenCalledWith([{ selected: [{ value: 'allow_once', label: '本次允许' }] }]);
  });

  it('leaves Enter on a focused button to that button', () => {
    const { controller, button } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    vi.advanceTimersByTime(ENTER_GUARD_MS);
    key('Enter', button('拒绝'));
    expect(controller.answer).not.toHaveBeenCalled();
  });

  it('sends one answer while it is on its way, whatever keys follow', () => {
    const { controller } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    controller.answer.mockImplementation(() => new Promise<void>(() => undefined));
    vi.advanceTimersByTime(ENTER_GUARD_MS);
    key('Escape');
    vi.advanceTimersByTime(ENTER_GUARD_MS);
    key('Enter');
    expect(controller.answer).toHaveBeenCalledTimes(1);
    expect(controller.answer).toHaveBeenCalledWith([{ selected: [{ value: 'reject', label: '拒绝' }] }]);
  });

  it('stops listening once answered', () => {
    const { controller, set } = mounted({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    set({ conversation: emptyConversation });
    key('Escape');
    expect(controller.answer).not.toHaveBeenCalled();
  });

  it('appears without taking the focus, and is read out', () => {
    const { set, input, announced } = mounted();
    input().focus();
    set({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    expect(document.activeElement === input() || document.activeElement === document.body).toBe(true);
    expect(announced()).toBe('需要你的授权才能继续');
  });
});

describe('the agent\'s question', () => {
  it('sends the options checked and the text typed', () => {
    const { root, controller, button } = mounted({ conversation: conversationOf(['chat.ask_user_question', {
      request_id: 'ask_1', source: 'ask_user_interrupt',
      questions: [{ question: '封面风格？', options: [{ label: '简洁' }, { label: '图片' }, { label: 'Other' }] }],
    }]) });
    expect([...root.querySelectorAll('.choice')].map(item => item.textContent)).toEqual(['简洁', '图片']);
    expect(button('提交').disabled).toBe(true);
    const second = root.querySelectorAll<HTMLInputElement>('.choice input')[1];
    second.checked = true;
    second.dispatchEvent(new Event('change'));
    root.querySelector<HTMLTextAreaElement>('.question-custom')!.value = ' 偏蓝色 ';

    button('提交').click();
    expect(controller.answer).toHaveBeenCalledWith([{ selected: [{ label: '图片' }], customInput: '偏蓝色' }]);
  });

  it('submits once while the answer is on its way, and again once it did not go out', async () => {
    const { root, controller, button } = mounted({ conversation: conversationOf(['chat.ask_user_question', {
      request_id: 'ask_1', source: 'ask_user_interrupt', questions: [{ question: '封面风格？', options: [{ label: '简洁' }] }],
    }]) });
    let fail: () => void = () => undefined;
    controller.answer.mockImplementation(() => new Promise<void>(resolve => { fail = resolve; }));
    const choice = root.querySelector<HTMLInputElement>('.choice input')!;
    choice.checked = true;
    choice.dispatchEvent(new Event('change'));
    button('提交').click();
    button('提交').click();
    expect(controller.answer).toHaveBeenCalledTimes(1);

    fail();
    await Promise.resolve();
    button('提交').click();
    expect(controller.answer).toHaveBeenCalledTimes(2);
  });
});

describe('the composer', () => {
  it('sends on Enter, and gives the text back when it did not go out', async () => {
    const { controller, input } = mounted();
    controller.send.mockResolvedValueOnce(false);
    input().value = '检查标题';
    key('Enter', input());
    expect(controller.send).toHaveBeenCalledWith('检查标题');
    expect(input().value).toBe('');
    await vi.waitFor(() => expect(input().value).toBe('检查标题'));
  });

  it('leaves the Enter that picks an input method candidate to the input method', () => {
    const { controller, input } = mounted();
    input().value = '检查标题';
    input().dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 229, bubbles: true, cancelable: true }));
    expect(controller.send).not.toHaveBeenCalled();
    expect(input().value).toBe('检查标题');
  });

  it('turns Send into Stop while a turn runs, and back into Send for a message typed meanwhile', () => {
    const { set, button, controller, input } = mounted();
    expect(button('发送').disabled).toBe(true);
    set({ conversation: conversationOf(['chat.processing_status', { is_processing: true }]) });
    button('停止').click();
    expect(controller.stop).toHaveBeenCalled();

    input().value = '顺便检查字号';
    input().dispatchEvent(new Event('input'));
    button('发送').click();
    expect(controller.send).toHaveBeenCalledWith('顺便检查字号');
  });

  it('offers no Stop in a window another one displaced, as the turn goes on there', () => {
    const { set, root } = mounted({ conversation: conversationOf(
      ['chat.processing_status', { is_processing: true }], ['chat.ask_user_question', PERMISSION]) });
    set({ attachment: { kind: 'displaced', sessionId: S1 } });
    expect(root.querySelector('.is-stop')).toBeNull();
  });

  it('waits for the connection before it sends again', () => {
    const { set, input } = mounted();
    set({ connection: { kind: 'down', attempt: 1, retryAt: clockNow + 1000 } });
    expect(input().disabled).toBe(true);
    expect(input().placeholder).toBe('连接恢复后即可发送');
    set({ connection: { kind: 'open' } });
    expect(input().disabled).toBe(false);
  });

  it('gives the focus back to the box a question took it from', () => {
    const { set, input } = mounted();
    input().focus();
    set({ conversation: conversationOf(['chat.ask_user_question', PERMISSION]) });
    // A browser drops the focus of a box it disables; this stands in for that.
    const elsewhere = document.body.appendChild(document.createElement('button'));
    elsewhere.focus();
    elsewhere.blur();
    expect(document.activeElement).toBe(document.body);
    set({ conversation: emptyConversation });
    expect(document.activeElement).toBe(input());
  });
});

describe('the tray', () => {
  it('counts down to the next reconnect, keeping the line and its focus', () => {
    const { set, text, second, root, announced } = mounted();
    set({ connection: { kind: 'down', attempt: 2, retryAt: clockNow + 5000 } });
    expect(text('.tray')).toBe('连接已中断，5 秒后重连（第 2 次）');
    expect(announced()).toBe('连接已中断，5 秒后重连（第 2 次）');
    const line = root.querySelector('.tray-item');
    second();
    expect(text('.tray')).toBe('连接已中断，4 秒后重连（第 2 次）');
    expect(root.querySelector('.tray-item')).toBe(line);
    expect(announced()).toBe('连接已中断，5 秒后重连（第 2 次）');
    set({ connection: { kind: 'open' } });
    expect(root.querySelector<HTMLElement>('.tray')!.hidden).toBe(true);
  });

  it('offers to continue here when another window holds the document, with the composer off', () => {
    const { set, button, input, controller, text } = mounted();
    set({ attachment: { kind: 'displaced', sessionId: S1 } });
    expect(input().disabled).toBe(true);
    expect(text('.tray')).toContain('已在另一个窗口中打开');
    button('在这里继续').click();
    expect(controller.continueHere).toHaveBeenCalled();
    set({ attachment: ATTACHED });
    expect(input().disabled).toBe(false);
    expect(text('.tray')).toBe('');
  });

  it('shows a failed attach and tries again on request', () => {
    const { set, button, text, controller } = mounted();
    set({ attachment: { kind: 'failed', sessionId: S1, error: new Error('服务暂不可用') } });
    expect(text('.tray')).toContain('无法连接演示文稿：服务暂不可用');
    button('重试').click();
    expect(controller.continueHere).toHaveBeenCalled();
  });

  it('shows what became of an action until dismissed', () => {
    const { set, button, text, controller } = mounted();
    set({ alert: { tone: 'error', text: '发送失败：网络中断' } });
    expect(text('.tray')).toBe('发送失败：网络中断');
    button('关闭提示').click();
    expect(controller.dismissAlert).toHaveBeenCalled();
  });
});

describe('the tool timeline', () => {
  const call = (id: string, name: string, args: JsonObject = {}, goal = ''): [string, JsonObject] => (
    ['chat.tool_call', { tool_call: { id, name, arguments: args, ...(goal ? { call_goal: goal } : {}) } }]);
  const result = (id: string, extra: JsonObject = {}): [string, JsonObject] => (
    ['chat.tool_result', { tool_result: { tool_call_id: id, status: 'success', ...extra } }]);

  it('shows the running step in one line with live seconds, its purpose on request, then a summary', () => {
    const running = conversationOf(['chat.processing_status', { is_processing: true }],
      call('c1', 'read_presentation_context', {}, '了解页数'), result('c1'), call('c2', 'list_slide_shapes', { slide_id: '258' }));
    const { set, root, text, second } = mounted({ conversation: running });
    const head = root.querySelector<HTMLButtonElement>('.activity-head')!;
    expect(head.getAttribute('aria-expanded')).toBe('false');
    expect(text('.activity-summary')).toBe('查看页面上的形状');
    head.click();
    expect(text('.step-goal')).toBe('了解页数');
    second();
    second();
    expect(text('.step:last-child .step-time')).toBe('2 秒');

    set({ conversation: applyEvent(running, { event: 'chat.tool_result', payload: { tool_result: {
      tool_call_id: 'c2', status: 'success' } } }, POWERPOINT.contract) });
    expect(text('.activity-summary')).toBe('查看过程');
    expect(root.querySelector('.activity-steps')!.hasAttribute('hidden')).toBe(false);
    root.querySelector<HTMLButtonElement>('.activity-head')!.click();
    expect(root.querySelector('.activity-steps')!.hasAttribute('hidden')).toBe(true);
  });

  it('keeps arguments hidden and opens one step\'s details at a time', () => {
    const { root } = mounted({ conversation: conversationOf(
      call('c1', 'list_slide_shapes', { slide_id: '258' }), result('c1'),
      call('c2', 'list_slide_shapes', { slide_id: '263' }), result('c2')) });
    root.querySelector<HTMLButtonElement>('.activity-head')!.click();
    const details = (): boolean[] => [...root.querySelectorAll('.step-detail')].map(node => !node.hasAttribute('hidden'));
    expect(details()).toEqual([false, false]);
    root.querySelectorAll<HTMLButtonElement>('button.step-head')[0].click();
    expect(details()).toEqual([true, false]);
    root.querySelectorAll<HTMLButtonElement>('button.step-head')[1].click();
    expect(details()).toEqual([false, true]);
    expect(root.querySelectorAll('.step-detail')[1].textContent).toContain('"slide_id": "263"');
  });

  it('shows one row per slide a check covers', () => {
    const { root } = mounted({ conversation: conversationOf(call('c1', 'verify_slides', { from_slide: 2, to_slide: 4 })) });
    expect([...root.querySelectorAll('.slide-rows li')].map(item => item.textContent)).toEqual(['第 3 页', '第 4 页', '第 5 页']);
  });

  it('folds a finished run to one quiet line, with no counts, failures or verdicts in it', () => {
    const { root, text } = mounted({ conversation: conversationOf(
      call('c1', 'read_presentation_context'), result('c1'),
      call('c2', 'presentation_apply_patch'), result('c2', { status: 'error' }),
      call('c3', 'verify_slide_visual', { slide_id: '258' }),
      result('c3', { result: '<verifier-result verdict="needs_work" slide_id="258">…</verifier-result>' })) });
    expect(text('.activity-head')).toBe('查看过程');
    expect(root.querySelector('.activity.has-failed, .activity .chip')).toBeNull();
    root.querySelector<HTMLButtonElement>('.activity-head')!.click();
    expect([...root.querySelectorAll('.step-title')].map(node => node.textContent)).toEqual(['读取演示文稿', '更新页面', '视觉复核']);
    expect(root.querySelector('.step.status-failed .step-title')!.textContent).toBe('更新页面');
  });

  it('folds a single finished step to its own name', () => {
    const { text } = mounted({ conversation: conversationOf(call('c1', 'read_presentation_context'), result('c1')) });
    expect(text('.activity-summary')).toBe('读取演示文稿');
  });

  it('names the document a task went to', () => {
    const { text } = mounted({ conversation: conversationOf(
      call('c9', 'session_send_message', { target_session_id: 's9', message: '同步标题样式' })) });
    expect(text('.step-title')).toBe('发给「市场分析.pptx」的任务');
  });
});

describe('the end of a turn', () => {
  it('reads the reply out', () => {
    const { set, announced } = mounted({ conversation: conversationOf(
      ['chat.processing_status', { is_processing: true }]) });
    set({ conversation: conversationOf(['chat.processing_status', { is_processing: true }],
      ['chat.final', { content: '标题改好了。' }], ['chat.processing_status', { is_processing: false }]) });
    expect(announced()).toBe('WorkSwarm 回复：标题改好了。');
  });

  const wrote = (): Conversation => conversationOf(['chat.processing_status', { is_processing: true }],
    ['chat.tool_call', { tool_call: { id: 'w1', name: 'presentation_apply_patch' } }],
    ['chat.tool_result', { tool_result: { tool_call_id: 'w1', status: 'success' } }],
    ['chat.tool_call', { tool_call: { id: 'w2', name: 'edit_slide_text' } }],
    ['chat.tool_result', { tool_result: { tool_call_id: 'w2', status: 'error' } }],
    ['chat.final', { content: '做好了。' }],
    ['chat.processing_status', { is_processing: false }]);

  const actions = (root: HTMLElement): (string | null)[] =>
    [...root.querySelectorAll('.turn-actions button')].map(item => item.getAttribute('aria-label'));

  it('ends a turn with icons under the reply: copy, and undo while the turn\'s changes can be undone', () => {
    const conversation = wrote();
    const card = conversation.entries.at(-1)!.id;
    const { controller, button, set, root } = mounted({ conversation });
    expect(actions(root)).toEqual(['复制']);

    set({ undo: { offers: new Set([card]) } });
    expect(actions(root)).toEqual(['复制', '撤销本轮修改']);
    expect(root.querySelector('.turn-actions')!.textContent).toBe('');
    expect(root.querySelector('.turn-actions')!.previousElementSibling!.textContent).toBe('做好了。');
    button('撤销本轮修改').click();
    expect(controller.undo).toHaveBeenCalledWith(card);
  });

  it('copies the whole reply of the turn, and shows for a moment that it did', async () => {
    const writeText = vi.fn(async (_text: string) => undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const { button, root } = mounted({ conversation: conversationOf(
      ['chat.processing_status', { is_processing: true }], ['chat.delta', { content: '先看一下。' }],
      ['chat.tool_call', { tool_call: { id: 'r1', name: 'read_presentation_context' } }],
      ['chat.tool_result', { tool_result: { tool_call_id: 'r1', status: 'success' } }],
      ['chat.final', { content: '共 4 页。' }], ['chat.processing_status', { is_processing: false }]) });
    button('复制').click();
    await vi.waitFor(() => expect(actions(root)).toEqual(['已复制']));
    expect(writeText).toHaveBeenCalledWith('先看一下。\n\n共 4 页。');
    vi.advanceTimersByTime(2000);
    expect(actions(root)).toEqual(['复制']);
  });

  it('shows dots while the agent works and nothing on screen moves', () => {
    const { set, root } = mounted();
    const dots = (): boolean => root.querySelector('.thinking') !== null;
    const events: [string, JsonObject][] = [['chat.processing_status', { is_processing: true }]];
    const step = (...more: [string, JsonObject][]): boolean => {
      events.push(...more);
      set({ conversation: conversationOf(...events) });
      return dots();
    };
    expect(step()).toBe(true);
    expect(step(['chat.tool_call', { tool_call: { id: 'r1', name: 'read_presentation_context' } }])).toBe(false);
    expect(step(['chat.tool_result', { tool_result: { tool_call_id: 'r1', status: 'success' } }])).toBe(true);
    expect(step(['chat.delta', { content: '共' }])).toBe(false);
    expect(step(['chat.final', { content: '共 4 页。' }], ['chat.processing_status', { is_processing: false }])).toBe(false);
  });

  it('asks in one short line before it overwrites slides changed since', () => {
    const conversation = wrote();
    const card = conversation.entries.at(-1)!.id;
    const { controller, button, root } = mounted({ conversation, undo: { offers: new Set([card]), confirm: card } });
    expect(root.querySelector('.transcript')!.lastElementChild!.textContent).toBe('后续修改会被覆盖取消仍然撤销');
    button('仍然撤销').click();
    expect(controller.undo).toHaveBeenCalledWith(card, true);
    button('取消').click();
    expect(controller.cancelUndo).toHaveBeenCalled();
  });
});

describe('the header', () => {
  it('lists the document\'s conversations, marks the current one, and switches', () => {
    const now = Math.floor(Date.now() / 1000);
    const { root, button, controller } = mounted({ conversations: [
      { sessionId: S1, title: '改标题', lastMessageAt: now - 300 },
      { sessionId: 's0' as SessionId, title: '', lastMessageAt: now - 3 * 86_400 },
    ] });
    button('本文档的对话').click();
    const items = [...root.querySelectorAll<HTMLButtonElement>('#history-menu .menu-item')];
    expect(items.map(item => item.querySelector('.menu-item-title')!.textContent)).toEqual(['改标题', '新对话']);
    expect(items.map(item => item.querySelector('.menu-item-meta')?.textContent)).toEqual(['5分钟', '3天']);
    expect(items[0].getAttribute('aria-current')).toBe('true');
    items[1].click();
    expect(controller.switchTo).toHaveBeenCalledWith('s0');
    expect(root.querySelector<HTMLElement>('#history-menu')!.hidden).toBe(true);
  });

  it('closes a menu on Esc and gives the focus back to its button', () => {
    const { root, button } = mounted();
    button('本文档的对话').click();
    key('Escape', root.querySelector('#history-menu')!);
    expect(root.querySelector<HTMLElement>('#history-menu')!.hidden).toBe(true);
    expect(document.activeElement).toBe(button('本文档的对话'));
  });

  it('downloads the conversation from More, once it has anything', async () => {
    const saved: Blob[] = [];
    Object.assign(URL, { createObjectURL: (blob: Blob) => { saved.push(blob); return 'blob:x'; }, revokeObjectURL: () => undefined });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    const { set, button } = mounted();
    button('更多').click();
    expect(button('下载对话记录').disabled).toBe(true);
    set({ conversation: conversationOf(['chat.final', { content: '好的。' }]) });
    button('下载对话记录').click();
    expect(click).toHaveBeenCalledTimes(1);
    const text = await new Promise<string>(resolve => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(saved[0]);
    });
    expect(text).toBe('# 季度汇报.pptx\n\n**WorkSwarm**\n\n好的。\n');
    click.mockRestore();
  });

  it('starts a new conversation', () => {
    const { button, controller } = mounted();
    button('新对话').click();
    expect(controller.newConversation).toHaveBeenCalled();
  });
});

describe('the model picker', () => {
  it('names the model under the message box and picks the one the next message runs on', () => {
    const { root, button, controller } = mounted({ models: ['jiuwen-pro', 'jiuwen-flash'], model: 'jiuwen-pro' });
    expect(root.querySelector('.model-name')!.textContent).toBe('jiuwen-pro');
    button('模型：jiuwen-pro').click();
    [...root.querySelectorAll<HTMLButtonElement>('#model-menu .menu-item')][1].click();
    expect(controller.chooseModel).toHaveBeenCalledWith('jiuwen-flash');
    expect(root.querySelector<HTMLElement>('#model-menu')!.hidden).toBe(true);
  });

  it('is not there while the server offers no model', () => {
    const { root } = mounted();
    expect(root.querySelector<HTMLElement>('.composer-foot')!.hidden).toBe(true);
  });
});

describe('reading out', () => {
  it('keeps the streamed reply out of the live region', () => {
    const { root, set } = mounted();
    set({ conversation: conversationOf(['chat.processing_status', { is_processing: true }],
      ['chat.delta', { content: '正在' }]) });
    expect(root.querySelector('.transcript')!.hasAttribute('aria-live')).toBe(false);
    expect(root.querySelector('[aria-live]')!.textContent).toBe('');
  });
});
