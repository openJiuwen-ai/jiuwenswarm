/**
 * What the pane shows of one conversation: its transcript, whether its turn
 * runs, and the question its turn waits on. A pure reducer over the
 * conversation's events, so a live turn and a replayed history read the same
 * way.
 */
import { copy } from './copy';
import {
  incomingMessage, incomingMessageStatus, MESSAGE_STATUS_EVENT, outgoingMessage,
  toolCallOf, toolResultCallId, toolResultFailed,
} from './crossDocumentMessages';
import { isRecord, type GatewayEvent, type JsonObject } from './gatewayClient';
import { isContractTool, isMutatingTool } from './presentationContract';

export type Entry =
  | { kind: 'user'; id: string; text: string }
  | { kind: 'assistant'; id: string; text: string }
  | {
    kind: 'tool'; id: string; name: string; status: 'running' | 'done' | 'failed';
    /** What the model said the call is for. */
    goal?: string;
    args?: JsonObject;
  }
  /** A task another document's conversation sent here; ``status`` is the mailbox's. */
  | { kind: 'incoming'; id: string; sourceTitle: string; text: string; status: string }
  /** A task this conversation sent to another document's. */
  | { kind: 'outgoing'; id: string; targetSessionId: string; text: string; status: 'sending' | 'sent' | 'failed' }
  | { kind: 'notice'; id: string; tone: 'info' | 'error'; text: string }
  /** The end of a turn that called a writing tool: the calls its undo takes back. */
  | { kind: 'changes'; id: string; callIds: string[] };

export interface PromptOption { label: string; value?: string; description?: string }

export interface PromptQuestion {
  question: string;
  header: string;
  options: PromptOption[];
  multiSelect: boolean;
  /** The approval card this question answers; the answer names it again. */
  cardId?: string;
  /** The arguments of the tool an approval is for. */
  toolArguments?: JsonObject;
  /** The tool an approval or a confirmation is for, as its header names it. */
  toolName?: string;
}

/** The interrupts answered with ``chat.send`` ``answers``: an approval, a confirmation, the agent's question. */
export type PromptSource = 'permission_interrupt' | 'confirm_interrupt' | 'ask_user_interrupt';
const PROMPT_SOURCES: ReadonlySet<string> = new Set<PromptSource>(
  ['permission_interrupt', 'confirm_interrupt', 'ask_user_interrupt']);

export interface Prompt {
  requestId: string;
  interruptId: string;
  source: PromptSource;
  questions: PromptQuestion[];
}

export interface Conversation {
  entries: readonly Entry[];
  turn: 'idle' | 'running';
  /**
   * The turn paused for this pane's tool calls: it still runs, as the pane answers them and the turn resumes.
   * Set by a ``chat.client_tool_request``, cleared when the turn resumes or ends.
   */
  awaitingTools: boolean;
  prompt?: Prompt;
  /** The assistant entry the stream writes to; a tool call or a final reply closes it. */
  openReply?: string;
  /** Where the turn the user still waits on began in ``entries``; a pause for a question does not end it. */
  turnStart?: number;
  nextId: number;
}

export const emptyConversation: Conversation = { entries: [], turn: 'idle', awaitingTools: false, nextId: 1 };

type WithoutId<E> = E extends Entry ? Omit<E, 'id'> & { id?: string } : never;
type NewEntry = WithoutId<Entry>;

function add(conversation: Conversation, entry: NewEntry): Conversation {
  const id = entry.id ?? `e${conversation.nextId}`;
  return {
    ...conversation, entries: [...conversation.entries, { ...entry, id } as Entry], nextId: conversation.nextId + 1,
  };
}

function replace(conversation: Conversation, id: string, update: (entry: Entry) => Entry): Conversation {
  return { ...conversation, entries: conversation.entries.map(entry => (entry.id === id ? update(entry) : entry)) };
}

function has(conversation: Conversation, id: string): boolean {
  return conversation.entries.some(entry => entry.id === id);
}

export function addUserMessage(conversation: Conversation, text: string): Conversation {
  // A new message ends the reply it follows; the web chat drops a pending question on a new query too.
  return add({ ...conversation, openReply: undefined, prompt: undefined }, { kind: 'user', text });
}

/** The conversation without one entry, as a message that did not go out. */
export function withoutEntry(conversation: Conversation, id: string): Conversation {
  return { ...conversation, entries: conversation.entries.filter(entry => entry.id !== id) };
}

export function addNotice(conversation: Conversation, tone: 'info' | 'error', text: string): Conversation {
  return add(conversation, { kind: 'notice', tone, text });
}

/** A turn starts running; one that paused for a question goes on. */
export function startTurn(conversation: Conversation): Conversation {
  return { ...conversation, turn: 'running', turnStart: conversation.turnStart ?? conversation.entries.length };
}

/** The turn is over here: the user stopped it, or it failed. Its steps still under way will not finish. */
export function endTurn(conversation: Conversation): Conversation {
  return closeTurn(unfinishedFailed(
    { ...conversation, turn: 'idle', awaitingTools: false, prompt: undefined, openReply: undefined }));
}

function unfinishedFailed(conversation: Conversation): Conversation {
  return { ...conversation, entries: conversation.entries.map(entry => (
    (entry.kind === 'tool' && entry.status === 'running') || (entry.kind === 'outgoing' && entry.status === 'sending')
      ? { ...entry, status: 'failed' } : entry)) };
}

/** The turn is over for the user: if it called a writing tool, its undo follows it. */
function closeTurn(conversation: Conversation): Conversation {
  const { turnStart } = conversation;
  if (turnStart === undefined) return conversation;
  const callIds = conversation.entries.slice(turnStart).flatMap(entry => (
    entry.kind === 'tool' && entry.status !== 'running' && isContractTool(entry.name) && isMutatingTool(entry.name)
      ? [entry.id] : []));
  const closed = { ...conversation, turnStart: undefined };
  return callIds.length ? add(closed, { kind: 'changes', callIds }) : closed;
}

export function clearPrompt(conversation: Conversation): Conversation {
  return { ...conversation, prompt: undefined };
}

/** One event of the conversation's live stream. */
export function applyEvent(conversation: Conversation, { event, payload }: GatewayEvent): Conversation {
  switch (event) {
    case 'chat.processing_status':
      return processingStatus(conversation, payload);
    case 'chat.client_tool_request':
      return { ...conversation, awaitingTools: true };
    case 'chat.ask_user_question': {
      const prompt = parsePrompt(payload);
      return prompt ? { ...conversation, prompt } : conversation;
    }
    case 'chat.interrupt_result':
      return payload.intent === 'cancel' && payload.success !== false ? endTurn(conversation) : conversation;
    case 'chat.error':
      return endTurn(addNotice(conversation, 'error', copy.turnError(errorText(payload))));
    default:
      return applyTranscriptEvent(conversation, event, payload);
  }
}

/** The events a live turn and a replayed history share: what was said and done. */
function applyTranscriptEvent(conversation: Conversation, event: string, payload: JsonObject): Conversation {
  switch (event) {
    case 'chat.delta':
      return typeof payload.content === 'string' ? reply(conversation, text => text + payload.content) : conversation;
    case 'chat.final': {
      const content = typeof payload.content === 'string' ? payload.content : '';
      // The backend sends a segment's final again when the turn pauses on a task pane tool.
      if (!conversation.openReply && content.trim() && content.trim() === lastReplyOfTurn(conversation)) {
        return conversation;
      }
      const replied = content ? reply(conversation, text => content || text) : conversation;
      return { ...replied, openReply: undefined };
    }
    case 'chat.tool_call':
      return toolCall({ ...conversation, openReply: undefined }, payload);
    case 'chat.tool_result':
      return toolResult(conversation, payload);
    case MESSAGE_STATUS_EVENT: {
      const message = incomingMessageStatus(payload);
      if (!message) return conversation;
      const id = incomingId(message.messageId);
      if (has(conversation, id)) {
        return replace(conversation, id, entry => (entry.kind === 'incoming' ? { ...entry, status: message.status } : entry));
      }
      return add(conversation, {
        kind: 'incoming', id, sourceTitle: message.sourceTitle, text: message.text, status: message.status,
      });
    }
    default:
      return conversation;
  }
}

function processingStatus(conversation: Conversation, payload: JsonObject): Conversation {
  if (payload.is_processing === true) {
    const message = incomingMessage(payload);
    const started = { ...startTurn(conversation), awaitingTools: false };
    // A turn another conversation's message started opens with that message.
    if (!message || has(started, incomingId(message.messageId))) return started;
    return add({ ...started, openReply: undefined }, {
      kind: 'incoming', id: incomingId(message.messageId), sourceTitle: message.sourceTitle,
      text: message.text, status: 'running',
    });
  }
  if (payload.is_processing === false && !conversation.awaitingTools) {
    const idle = { ...conversation, turn: 'idle' as const, openReply: undefined };
    return conversation.prompt ? idle : closeTurn(idle);
  }
  return conversation;
}

/** The text of the latest reply since the user last spoke, trimmed; ``''`` when there is none. */
function lastReplyOfTurn({ entries }: Conversation): string {
  for (let at = entries.length - 1; at >= 0; at -= 1) {
    const entry = entries[at];
    if (entry.kind === 'user') return '';
    if (entry.kind === 'assistant') return entry.text.trim();
  }
  return '';
}

function reply(conversation: Conversation, write: (text: string) => string): Conversation {
  const { openReply } = conversation;
  if (openReply) {
    return replace(conversation, openReply, entry => (entry.kind === 'assistant' ? { ...entry, text: write(entry.text) } : entry));
  }
  const text = write('');
  if (!text.trim()) return conversation;
  const opened = add(conversation, { kind: 'assistant', text });
  return { ...opened, openReply: opened.entries.at(-1)!.id };
}

/** The tool a deferred tool runs through; the same call comes again as ``<id>:target`` under the tool's own name. */
const DEFERRED_DISPATCH = 'tool_call';

function toolCall(conversation: Conversation, payload: JsonObject): Conversation {
  const { callId, name, args, goal } = toolCallOf(payload);
  if (!callId || !name || name === DEFERRED_DISPATCH || has(conversation, callId)) return conversation;
  const outgoing = outgoingMessage(payload);
  if (outgoing) {
    return add(conversation, {
      kind: 'outgoing', id: outgoing.callId, targetSessionId: outgoing.targetSessionId, text: outgoing.text,
      status: 'sending',
    });
  }
  return add(conversation, {
    kind: 'tool', id: callId, name, status: 'running',
    ...(goal ? { goal } : {}), ...(Object.keys(args).length ? { args } : {}),
  });
}

function toolResult(conversation: Conversation, payload: JsonObject): Conversation {
  const failed = toolResultFailed(payload);
  return replace(conversation, toolResultCallId(payload), entry => {
    if (entry.kind === 'tool') return { ...entry, status: failed ? 'failed' : 'done' };
    if (entry.kind === 'outgoing') return { ...entry, status: failed ? 'failed' : 'sent' };
    return entry;
  });
}

function incomingId(messageId: string): string {
  return `incoming:${messageId}`;
}

function errorText(payload: JsonObject): string {
  const message = payload.error ?? payload.message;
  return typeof message === 'string' && message ? message : copy.turnErrorFallback;
}

/** The question a ``chat.ask_user_question`` asks, or undefined for one this pane does not answer. */
export function parsePrompt(payload: JsonObject): Prompt | undefined {
  const { request_id: requestId, source } = payload;
  if (typeof requestId !== 'string' || !requestId || typeof source !== 'string' || !PROMPT_SOURCES.has(source)) {
    return undefined;
  }
  const questions = (Array.isArray(payload.questions) ? payload.questions : []).filter(isRecord).flatMap(
    (raw): PromptQuestion[] => (typeof raw.question === 'string' ? [{
      question: raw.question,
      header: typeof raw.header === 'string' ? raw.header : '',
      multiSelect: raw.multi_select === true,
      options: (Array.isArray(raw.options) ? raw.options : []).filter(isRecord).flatMap(
        (option): PromptOption[] => (typeof option.label === 'string' ? [{
          label: option.label,
          ...(typeof option.value === 'string' && option.value ? { value: option.value } : {}),
          ...(typeof option.description === 'string' && option.description ? { description: option.description } : {}),
        }] : [])),
      ...(typeof raw.card_id === 'string' && raw.card_id ? { cardId: raw.card_id } : {}),
      ...(isRecord(raw.tool_payload) ? { toolArguments: raw.tool_payload } : {}),
      ...headerToolName(raw.header),
    }] : []));
  if (!questions.length) return undefined;
  return {
    requestId, source: source as PromptSource, questions,
    interruptId: typeof payload.interrupt_id === 'string' ? payload.interrupt_id : '',
  };
}

/** The backend titles an approval ``权限审批: <tool>`` and a confirmation ``操作确认: <tool>``. */
function headerToolName(header: unknown): { toolName?: string } {
  const name = typeof header === 'string' ? /^(?:权限审批|操作确认)\s*[:：]\s*(\S+)\s*$/.exec(header)?.[1] : undefined;
  return name ? { toolName: name } : {};
}

/** What the user chose for one question of a prompt. */
export interface PromptChoice { selected: PromptOption[]; customInput?: string }

/** The ``answers`` of the ``chat.send`` that answers ``prompt``, as the web chat sends them. */
export function promptAnswers(prompt: Prompt, choices: readonly PromptChoice[]): JsonObject[] {
  return prompt.questions.map((question, index) => {
    const { selected, customInput } = choices[index] ?? { selected: [] };
    return {
      question: question.question,
      selected_options: selected.map(option => option.value || option.label),
      ...(customInput ? { custom_input: customInput } : {}),
      ...(question.cardId ? { card_id: question.cardId } : {}),
    };
  });
}

/**
 * A conversation's history (``client.conversation.messages``, oldest first) as the pane shows it.
 * It only shows what happened: a question asked back then shows as a note and never asks again,
 * and nothing in it starts or ends a turn.
 */
export function replayHistory(messages: readonly JsonObject[]): Conversation {
  let conversation = emptyConversation;
  for (const message of messages) {
    if (message.role === 'user') {
      conversation = replayedUserMessage(conversation, message);
      continue;
    }
    const event = typeof message.event_type === 'string' && message.event_type ? message.event_type : 'chat.final';
    const note = replayedQuestionText(event, message);
    conversation = note
      ? addNotice(conversation, 'info', note)
      : applyTranscriptEvent(conversation, event, message);
  }
  return unfinishedFailed({ ...conversation, openReply: undefined });
}

function replayedUserMessage(conversation: Conversation, message: JsonObject): Conversation {
  const text = typeof message.content === 'string' ? message.content : '';
  const incoming = incomingMessage(message);
  if (!incoming) return add(conversation, { kind: 'user', text });
  if (has(conversation, incomingId(incoming.messageId))) return conversation;
  return add(conversation, {
    kind: 'incoming', id: incomingId(incoming.messageId), sourceTitle: incoming.sourceTitle,
    text: incoming.text || text, status: 'succeeded',
  });
}

/** The note a replayed question or its answer shows, or undefined. */
function replayedQuestionText(event: string, message: JsonObject): string | undefined {
  const record = eventRecord(message);
  if (event === 'chat.ask_user_question') {
    const questions = list(record.questions)
      .map(item => (isRecord(item) && typeof item.question === 'string' ? item.question.trim() : ''))
      .filter(Boolean);
    return questions.length ? copy.askedInHistory(questions.join('；')) : undefined;
  }
  if (event === 'chat.ask_user_answer') {
    const answers = list(record.answers).map(item => {
      if (!isRecord(item)) return '';
      const selected = list(item.selected_options).filter((option): option is string => typeof option === 'string');
      const custom = typeof item.custom_input === 'string' ? item.custom_input : '';
      return [...selected, custom].map(value => value.trim()).filter(Boolean).join('、');
    }).filter(Boolean);
    return answers.length ? copy.answeredInHistory(answers.join('；')) : undefined;
  }
  return undefined;
}

/** History keeps an event's own fields on the record, or under ``event`` / ``event_payload``. */
function eventRecord(message: JsonObject): JsonObject {
  const wrapped = isRecord(message.event_payload) ? message.event_payload : undefined;
  if (wrapped) return isRecord(wrapped.event) ? wrapped.event : wrapped;
  return isRecord(message.event) ? message.event : message;
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}
