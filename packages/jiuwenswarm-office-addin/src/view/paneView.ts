import type { AttachmentState, SessionId } from '../clientAttachment';
import type { Entry, Prompt } from '../conversationModel';
import { copy, type DocumentCopy } from '../copy';
import type { HostLine } from '../hostLine';
import { busy, type PaneState, type TaskPaneController } from '../paneController';
import { transcriptFileName, transcriptMarkdown } from '../transcriptMarkdown';
import { Composer, type Waiting } from './composer';
import { saveFile } from './download';
import { h } from './dom';
import { Header } from './header';
import type { ToolCards } from './editCards';
import { approvalSheet, questionCard, type Interaction } from './interaction';
import { Transcript } from './transcript';
import { Tray, trayItems, type TrayItem } from './tray';

type Controller = Pick<
  TaskPaneController, 'state' | 'subscribe' | 'send' | 'stop' | 'answer' | 'switchTo' | 'newConversation'
  | 'continueHere' | 'chooseModel' | 'dismissAlert' | 'undo' | 'cancelUndo' | 'retry' | 'sendNow' | 'unqueue'>;

/** How long a running turn goes without news before the pane says it is still working. */
const STALL_MS = 15_000;

/** The longest reply the pane reads out when a turn ends; the transcript has the rest. */
const ANNOUNCED_REPLY_CHARS = 200;

export interface PaneClock {
  now: () => number;
  every: (ms: number, tick: () => void) => void;
}

const wallClock: PaneClock = { now: () => Date.now(), every: (ms, tick) => void setInterval(tick, ms) };

/**
 * Render the pane into ``root`` and keep it in step with the controller, in
 * ``words`` that name the host's document, with the host's ``cards`` under its
 * calls, its ``waiting`` line in the tray and its ``selection`` line above
 * the message box. The clock moves the running steps' seconds and the
 * reconnect countdown on once a second.
 */
export function mountPane(
  root: HTMLElement, controller: Controller, words: DocumentCopy,
  { clock = wallClock, cards, waiting: waitingLine, selection }: {
    clock?: PaneClock; cards?: ToolCards; waiting?: HostLine; selection?: HostLine;
  } = {},
): void {
  const header = new Header(words.noun, {
    switchTo: sessionId => void controller.switchTo(sessionId),
    newConversation: () => void controller.newConversation(),
    downloadTranscript: () => {
      const { attachment, conversation, documents } = controller.state();
      const name = attachment.kind === 'attached' ? attachment.displayName : '';
      saveFile(transcriptFileName(name, new Date()),
        transcriptMarkdown(name || copy.untitledConversation, conversation.entries, documents));
    },
  });
  const composer = new Composer(words.noun, {
    send: (text, files) => controller.send(text, files), stop: () => void controller.stop(),
    chooseModel: model => controller.chooseModel(model),
    sendNow: id => void controller.sendNow(id), unqueue: id => controller.unqueue(id),
  });
  const transcript = new Transcript(words, text => composer.fill(text), {
    undo: (...choice) => void controller.undo(...choice),
    cancelUndo: () => controller.cancelUndo(),
    retry: () => void controller.retry(),
    newConversation: () => void controller.newConversation(),
  }, cards);
  const tray = new Tray();
  const interactionSlot = h('div', { class: 'interaction-slot' });
  const selectionNode = h('p', { class: 'selection-line', hidden: true });
  const paintSelection = (): void => {
    selectionNode.textContent = selection?.text() ?? '';
    selectionNode.hidden = !selectionNode.textContent;
  };
  // Not the transcript: a streamed reply would be read out again on every delta.
  const announcer = h('div', { class: 'sr-only', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' });
  root.replaceChildren(h('main', { class: 'pane' },
    header.element, transcript.element, h('div', { class: 'dock' }, tray.element, interactionSlot, selectionNode,
      composer.element)),
  announcer);

  const announce = (text: string): void => {
    announcer.textContent = text;
  };
  const trayActions = { continueHere: () => void controller.continueHere(), dismissAlert: () => controller.dismissAlert() };
  let announcedTray = new Map<string, string>();
  /** The tray line the announcer last read out. */
  let spoken: TrayItem | undefined;
  const paintTray = (state: PaneState, now: number): void => {
    const items = trayItems({ ...state, waiting: waitingLine?.text() }, now, trayActions, words.noun);
    tray.update(items);
    // A line is read out when it appears or says something new; a countdown's ticks are not news.
    const news = items.find(item => announcedTray.get(item.key) !== shape(item));
    if (news) {
      announce(news.text);
      spoken = news;
    } else if (spoken && !items.some(({ key }) => key === spoken?.key)) {
      // A line gone from the tray, as the connection's once it is back, is not left to be read in the announcer.
      if (announcer.textContent === spoken.text) announce('');
      spoken = undefined;
    }
    announcedTray = new Map(items.map(item => [item.key, shape(item)]));
  };

  let interaction: { prompt: Prompt; view: Interaction } | undefined;
  let last: PaneState | undefined;
  /** When the shown conversation last changed: an event of its turn came, or the user sent. */
  let heardAt = clock.now();
  let stalled = false;
  const render = (state: PaneState): void => {
    const { attachment, conversation } = state;
    if (conversation !== last?.conversation) heardAt = clock.now();
    stalled = clock.now() - heardAt >= STALL_MS;
    const attached = attachment.kind === 'attached';
    const running = busy(state);
    header.update({
      conversations: state.conversations, shown: shownSession(attachment), enabled: attachment.kind !== 'detached',
      busy: running, canDownload: conversation.entries.length > 0, documents: state.documents,
    });
    transcript.update({
      entries: conversation.entries, documents: state.documents, openReply: conversation.openReply,
      working: attached && conversation.turn === 'running' && !conversation.prompt,
      hint: conversation.compacting ? copy.compacting : stalled ? copy.stalled : undefined,
      loadingHistory: state.loadingHistory, canSuggest: attached && !conversation.prompt, undo: state.undo,
      retry: state.retry,
    });
    if (state.connection !== last?.connection || attachment !== last?.attachment || state.alert !== last?.alert) {
      paintTray(state, clock.now());
    }
    const prompt = attached ? conversation.prompt : undefined;
    if (prompt !== interaction?.prompt) {
      interaction?.view.dispose();
      interaction = prompt ? { prompt, view: promptView(prompt, state, choices => controller.answer(choices)) }
        : undefined;
      interactionSlot.replaceChildren(...(interaction ? [interaction.view.element] : []));
      if (prompt) announce(isApproval(prompt) ? copy.approvalNeeded : copy.questionAsked);
    }
    composer.update({
      enabled: attached && state.connection.kind === 'open' && !conversation.prompt && !state.loadingHistory,
      running,
      waiting: waiting(state),
      models: state.models,
      model: state.model,
      queue: state.queue,
    });
    if (last && !state.loadingHistory && !last.loadingHistory && conversation !== last.conversation) {
      const said = news(last.conversation, conversation);
      if (said) announce(said);
    }
    last = state;
  };
  render(controller.state());
  controller.subscribe(render);
  cards?.subscribe(() => render(controller.state()));
  waitingLine?.subscribe(() => paintTray(controller.state(), clock.now()));
  paintSelection();
  selection?.subscribe(paintSelection);
  clock.every(1000, () => {
    const now = clock.now();
    transcript.tick(now);
    if (last && !stalled && now - heardAt >= STALL_MS) render(last);
    if (last?.connection.kind === 'down') paintTray(last, now);
  });
}

function shownSession(attachment: AttachmentState): SessionId | undefined {
  return attachment.kind === 'detached' ? undefined : attachment.sessionId;
}

function waiting({ attachment, connection, conversation }: PaneState): Waiting {
  if (connection.kind === 'down') return 'offline';
  if (attachment.kind === 'displaced') return 'displaced';
  if (attachment.kind !== 'attached') return 'document';
  return conversation.prompt ? 'prompt' : 'history';
}

function isApproval(prompt: Prompt): boolean {
  return prompt.source !== 'ask_user_interrupt';
}

function promptView(prompt: Prompt, state: PaneState, answer: Parameters<typeof questionCard>[1]): Interaction {
  return isApproval(prompt) ? approvalSheet(prompt, state.documents, answer) : questionCard(prompt, answer);
}

/** What a tray line says, apart from the numbers a countdown changes. */
function shape(item: TrayItem): string {
  return `${item.tone}:${item.text.replace(/\d+/g, '#')}`;
}

/** What to read out about a change of the transcript: a new error, or the reply a turn ended with. */
function news(
  before: { entries: readonly Entry[]; turn: string }, after: { entries: readonly Entry[]; turn: string },
): string | undefined {
  const error = after.entries.slice(before.entries.length).find(entry => entry.kind === 'notice' && entry.tone === 'error');
  if (error?.kind === 'notice') return error.text;
  if (before.turn !== 'running' || after.turn !== 'idle') return undefined;
  const reply = [...after.entries].reverse().find(entry => entry.kind === 'assistant');
  return reply?.kind === 'assistant' && reply.text ? copy.replied(reply.text.slice(0, ANNOUNCED_REPLY_CHARS)) : undefined;
}
