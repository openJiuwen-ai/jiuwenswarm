/**
 * The task pane's state and the user's actions on it.
 *
 * The attachment owns which conversation the pane shows and whether it holds
 * the document; this controller derives the rest from it: the transcript of
 * the shown conversation (its history, then its live events), the document's
 * conversations, and the names of the user's other open documents.
 */
import type { AttachmentState, ClientAttachment, SessionId } from './clientAttachment';
import {
  addNotice, addUserMessage, applyEvent, clearPrompt, emptyConversation, endTurn, promptAnswers, replayHistory,
  startTurn, withoutEntry, type Conversation, type PromptChoice,
} from './conversationModel';
import { copy, reasonOf } from './copy';
import type { DocumentId } from './documentIdentity';
import type { ConnectionState, GatewayClient, GatewayEvent } from './gatewayClient';
import type { ToolContract } from './appContract';
import {
  attachmentBytes, imageDataUrl, isImage, type OpenDocument, type OutgoingAttachment, type ResourceConversation, type SessionFacade,
} from './sessionFacade';
import type { SlideSnapshots } from './slideSnapshots';

/** A message sent while a turn ran: it goes when the turn ends, or now as the turn's supplement. */
export interface QueuedMessage { id: string; text: string; attachments: readonly OutgoingAttachment[] }

/** The turns the transcript offers to undo, by their ``changes`` entry. */
export interface UndoState {
  /** None while a turn runs. */
  offers: ReadonlySet<string>;
  /** The undo that waits for the user's word: slides changed since would be overwritten. */
  confirm?: string;
}

export interface PaneState {
  attachment: AttachmentState;
  conversation: Conversation;
  /** The document's conversations, the most recently active first. */
  conversations: readonly ResourceConversation[];
  /** The user's attached documents, by their conversation. */
  documents: ReadonlyMap<string, OpenDocument>;
  /** The shown conversation's history is on its way; its live events wait for it. */
  loadingHistory: boolean;
  connection: ConnectionState;
  /** What became of the user's last action when the transcript has no place for it, until dismissed. */
  alert?: { tone: 'info' | 'error'; text: string };
  /** The models a message can run on, and the one the next message runs on. */
  models: readonly string[];
  model?: string;
  undo: UndoState;
  /** The failure notice whose message can go again: the latest entry, while no turn runs. */
  retry?: string;
  /** The messages waiting for the shown conversation's turn to end, the next first; a reload of the pane drops them. */
  queue: readonly QueuedMessage[];
}

const HISTORY_RETRY_MS = 1500;

type Attachment = Pick<
  ClientAttachment, 'current' | 'sessionId' | 'start' | 'attach' | 'onEvent' | 'onStateChange' | 'cancelToolCalls'>;
type Sessions = Pick<
  SessionFacade, 'create' | 'models' | 'conversations' | 'history' | 'processing' | 'openDocuments'
  | 'sendText' | 'interrupt' | 'answer'>;

export class TaskPaneController {
  private value: PaneState;
  /** The conversation whose transcript the pane shows. */
  private shown: SessionId | undefined;
  /** The connection dropped since the transcript was loaded, so events of its turn may have gone missing. */
  private missedEvents = false;
  /** Counts the transcript resets; a history load shows its answer only if no reset came after it. */
  private loads = 0;
  /** Live events of the shown conversation that arrived before its history. */
  private held: GatewayEvent[] = [];
  /** The line the next message starts with after an undo, as the agent's slide ids are stale. */
  private undoNote: string | undefined;
  /** An undo is under way; a second click waits for none. */
  private undoing = false;
  /** A conversation nobody has written in: created by this pane, or its history read back with no messages. */
  private unwritten: SessionId | undefined;
  /** A new conversation being created; a click meanwhile waits for it instead of creating another. */
  private creating: Promise<void> | undefined;
  /** The last message this pane sent, by its transcript entry, for a retry to send again. */
  private lastMessage: { entryId: string; text: string; attachments: readonly OutgoingAttachment[] } | undefined;
  private queued = 0;
  private readonly listeners = new Set<(state: PaneState) => void>();

  constructor(private readonly options: {
    attachment: Attachment; sessions: Sessions; documentId: DocumentId;
    connection: Pick<GatewayClient, 'onConnectionChange'>;
    contract: Pick<ToolContract, 'isMutating'>;
    /** The turns' undo; PowerPoint's alone, so a pane without it offers none. */
    snapshots?: Pick<SlideSnapshots, 'closeTurn' | 'undoable' | 'undo'>;
  }) {
    const { attachment } = options;
    this.value = {
      attachment: attachment.current(), conversation: emptyConversation, conversations: [], documents: new Map(),
      loadingHistory: false, connection: { kind: 'open' }, models: [], undo: { offers: new Set() }, queue: [],
    };
    attachment.onStateChange(state => this.attachmentChanged(state));
    attachment.onEvent(event => this.received(event));
    options.connection.onConnectionChange(connection => {
      if (connection.kind === 'down') this.missedEvents = true;
      this.update({ connection });
    });
  }

  state(): PaneState {
    return this.value;
  }

  subscribe(listener: (state: PaneState) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Attach the document to its most recent conversation, or to a new one. */
  async open(): Promise<void> {
    void this.loadModels();
    let latest: SessionId | undefined;
    try {
      [latest] = (await this.refreshConversations()).map(item => item.sessionId);
    } catch {
      // Without the list the pane still works: it starts a new conversation.
    }
    await this.options.attachment.start(latest);
  }

  /**
   * A new conversation of this document; the one it leaves stays in the list, its running turn stopped. One nobody
   * has written in is kept.
   */
  async newConversation(): Promise<void> {
    if (this.shown && this.shown === this.unwritten) return;
    if (busy(this.value) && !await this.stop()) return;
    this.creating ??= this.createConversation().finally(() => { this.creating = undefined; });
    await this.creating;
  }

  private async createConversation(): Promise<void> {
    const { sessions, attachment } = this.options;
    let fresh: SessionId;
    try {
      fresh = await sessions.create(this.value.model ?? (await sessions.models()).active);
    } catch (error) {
      this.update({ alert: { tone: 'error', text: copy.newConversationFailed(reasonOf(error)) } });
      return;
    }
    this.unwritten = fresh;
    await attachment.attach(fresh);
  }

  /** Show another conversation of this document; the shown one's running turn stops first, as nobody would see it. */
  async switchTo(sessionId: SessionId): Promise<void> {
    if (sessionId === this.shown || (busy(this.value) && !await this.stop())) return;
    await this.options.attachment.attach(sessionId);
  }

  /** Take the document back from the window that displaced this one, or try a failed attach again. */
  async continueHere(): Promise<void> {
    const sessionId = this.options.attachment.sessionId();
    if (sessionId) await this.options.attachment.attach(sessionId);
    else await this.newConversation();
  }

  /** The model the next message runs on. */
  chooseModel(model: string): void {
    this.update({ model });
  }

  dismissAlert(): void {
    this.update({ alert: undefined });
  }

  /** Send a message and its files; while the turn runs, it waits in the queue for the turn to end. */
  async send(text: string, attachments: readonly OutgoingAttachment[] = []): Promise<boolean> {
    const { attachment, conversation } = this.value;
    if ((!text.trim() && !attachments.length) || attachment.kind !== 'attached' || conversation.prompt) return false;
    if (conversation.turn !== 'running') return this.deliver(text, attachments);
    this.update({ queue: [...this.value.queue, { id: `q${++this.queued}`, text, attachments }] });
    return true;
  }

  /**
   * Send a waiting message now. While the turn runs it stops the turn and the next one takes the text as a
   * supplement, which carries no files, so a message with files waits for the turn to end. One that does not go
   * out goes back to the head of the queue, unless the pane shows another conversation by then.
   */
  async sendNow(id: string): Promise<void> {
    const { conversation } = this.value;
    const item = this.value.queue.find(queued => queued.id === id);
    // A question waiting on the user is answered first: a supplement would drop it while the turn still waits.
    if (!item || conversation.prompt || (conversation.turn === 'running' && item.attachments.length)) return;
    const from = this.shown;
    this.unqueue(id);
    if (!await this.deliver(item.text, item.attachments) && this.shown === from) {
      this.update({ queue: [item, ...this.value.queue] });
    }
  }

  /** Take a message out of the queue, as to edit it in the box again. */
  unqueue(id: string): QueuedMessage | undefined {
    const item = this.value.queue.find(queued => queued.id === id);
    if (item) this.update({ queue: this.value.queue.filter(queued => queued !== item) });
    return item;
  }

  /**
   * Send now, as a supplement while the turn runs. After an undo the message starts with a line saying so, as the
   * agent's slide ids went stale.
   */
  private async deliver(text: string, attachments: readonly OutgoingAttachment[]): Promise<boolean> {
    const content = text.trim();
    const { attachment } = this.value;
    const running = this.value.conversation.turn === 'running';
    if (attachment.kind !== 'attached') return false;
    const { sessionId } = attachment;
    const note = this.undoNote;
    const message = note ? `${note}\n${content}` : content;
    const sent = addUserMessage(running ? this.value.conversation : startTurn(this.value.conversation), content,
      attachments.filter(isImage).map(image => ({ filename: image.filename, src: imageDataUrl(image) })),
      attachments.filter(item => !isImage(item)).map(item => ({ filename: item.filename, size: attachmentBytes(item) })));
    const messageId = sent.entries[sent.entries.length - 1].id;
    this.lastMessage = { entryId: messageId, text, attachments };
    this.update({ conversation: sent, alert: undefined });
    try {
      if (running) {
        await this.options.sessions.interrupt(sessionId, 'supplement', message, this.value.model);
        // The next turn starts over from the message; a write the stopped turn still had queued would surprise it.
        this.options.attachment.cancelToolCalls();
      } else {
        await this.options.sessions.sendText(sessionId, message, attachments, this.value.model);
      }
      if (this.undoNote === note) this.undoNote = undefined;
      if (this.unwritten === sessionId) this.unwritten = undefined;
      return true;
    } catch (error) {
      if (sessionId !== this.shown) return false;
      // The message goes back to the composer, so the transcript does not keep it too.
      const kept = withoutEntry(this.value.conversation, messageId);
      this.update({
        conversation: running ? kept : endTurn(kept, this.options.contract),
        alert: { tone: 'error', text: copy.sendFailed(reasonOf(error)) },
      });
      return false;
    }
  }

  /** Send the message of the failed turn again, while the failure offers it. */
  async retry(): Promise<void> {
    if (this.value.retry && this.lastMessage) await this.send(this.lastMessage.text, this.lastMessage.attachments);
  }

  /**
   * Stop the running turn and drop the messages waiting for it; its tool calls that have not started do not run.
   * False when the stop did not go out.
   */
  async stop(): Promise<boolean> {
    const sessionId = this.options.attachment.sessionId();
    if (!sessionId) return false;
    try {
      await this.options.sessions.interrupt(sessionId, 'cancel');
    } catch (error) {
      if (sessionId === this.shown) this.update({ alert: { tone: 'error', text: copy.stopFailed(reasonOf(error)) } });
      return false;
    }
    this.options.attachment.cancelToolCalls();
    if (sessionId === this.shown) this.update({ queue: [] });
    this.updateShown(
      sessionId, conversation => endTurn(addNotice(conversation, 'info', copy.stopped), this.options.contract));
    return true;
  }

  /** Answer the question the turn waits on, one choice per question. */
  async answer(choices: readonly PromptChoice[]): Promise<void> {
    const { prompt } = this.value.conversation;
    const sessionId = this.options.attachment.sessionId();
    if (!prompt || !sessionId) return;
    try {
      await this.options.sessions.answer(sessionId, prompt, promptAnswers(prompt, choices));
    } catch (error) {
      // The answer did not go out, so the card stays to answer again; a refused one ends the turn with chat.error.
      if (sessionId === this.shown) this.update({ alert: { tone: 'error', text: copy.answerFailed(reasonOf(error)) } });
      return;
    }
    this.updateShown(sessionId, conversation => (conversation.prompt === prompt ? clearPrompt(conversation) : conversation));
  }

  /**
   * Undo the turn a ``changes`` entry closes, and every later one. When slides changed since would be overwritten
   * it asks first (``undo.confirm``); ``overwrite`` is the user's yes.
   */
  async undo(card: string, overwrite = false): Promise<void> {
    const entry = this.value.conversation.entries.find(item => item.id === card);
    const { snapshots } = this.options;
    if (entry?.kind !== 'changes' || !snapshots || !this.value.undo.offers.has(card) || this.undoing) return;
    this.undoing = true;
    this.update({ undo: { ...this.value.undo, confirm: undefined } });
    let result;
    try {
      result = await snapshots.undo(entry.callIds, overwrite);
    } catch (error) {
      this.update({ alert: { tone: 'error', text: copy.undoFailed(reasonOf(error)) } });
      return;
    } finally {
      this.undoing = false;
    }
    if (result.kind === 'changed') {
      this.update({ undo: { ...this.value.undo, confirm: card } });
    } else if (result.kind === 'gone') {
      this.update({ alert: { tone: 'info', text: copy.undoGone } });
    } else {
      this.undoNote = copy.undoNote(result.positions);
      this.update({});
    }
  }

  cancelUndo(): void {
    this.update({ undo: { ...this.value.undo, confirm: undefined } });
  }

  private attachmentChanged(state: AttachmentState): void {
    const sessionId = state.kind === 'detached' ? undefined : state.sessionId;
    // Attached again, as after a reconnect or "continue here": what the turn did meanwhile is only in the history.
    // The attachment id cannot tell, as every attach of the document by its user gets the same one.
    const reattached = state.kind === 'attached' && (this.missedEvents || this.value.attachment.kind !== 'attached');
    if (state.kind === 'attached') this.missedEvents = false;
    if (sessionId === this.shown && !reattached) {
      this.update({ attachment: state });
      return;
    }
    const { turn, prompt } = this.value.conversation;
    const same = sessionId === this.shown;
    const wasBusy = same && (turn === 'running' || prompt !== undefined);
    this.shown = sessionId;
    this.loads += 1;
    this.held = [];
    this.lastMessage = undefined;
    this.update({
      attachment: state, conversation: same ? this.value.conversation : emptyConversation,
      loadingHistory: sessionId !== undefined, alert: undefined, queue: [],
    });
    if (!sessionId) return;
    void this.loadHistory(sessionId, wasBusy);
    void this.refreshDocuments(sessionId);
    // The attachment may have started this conversation itself, as when the one it had was refused.
    void this.refreshConversations().catch(() => undefined);
  }

  /**
   * Show the conversation's history, and its turn as running while the server still runs it; history alone cannot
   * tell. A turn the pane showed running (``wasBusy``) that no longer runs ended out of its sight. A history that
   * cannot be read, after one more try, leaves the transcript shown before the reload.
   */
  private async loadHistory(sessionId: SessionId, wasBusy: boolean): Promise<void> {
    const load = this.loads;
    const processing = this.options.sessions.processing(sessionId).catch(() => undefined);
    let conversation = this.value.conversation;
    try {
      const messages = await this.options.sessions.history(sessionId).catch(async () => {
        await new Promise(resolve => setTimeout(resolve, HISTORY_RETRY_MS));
        return this.options.sessions.history(sessionId);
      });
      conversation = replayHistory(messages);
      if (!messages.length && load === this.loads) this.unwritten = sessionId;
    } catch (error) {
      if (load === this.loads) this.update({ alert: { tone: 'error', text: copy.historyFailed(reasonOf(error)) } });
    }
    const running = await processing;
    if (load !== this.loads) return;
    if (running) conversation = startTurn(conversation);
    else if (running === false && wasBusy) {
      conversation = addNotice(endTurn(conversation, this.options.contract), 'info', copy.endedWhileAway);
    }
    const held = this.held;
    this.held = [];
    this.update({
      conversation: held.reduce((shown, event) => applyEvent(shown, event, this.options.contract), conversation),
      loadingHistory: false,
    });
  }

  private received(event: GatewayEvent): void {
    if (event.event === 'client.attachments_changed') {
      if (this.shown) void this.refreshDocuments(this.shown);
      return;
    }
    if (event.event === 'models.updated') {
      void this.loadModels();
      return;
    }
    if (this.value.loadingHistory) {
      this.held.push(event);
      return;
    }
    const before = this.value.conversation;
    const conversation = applyEvent(before, event, this.options.contract);
    this.update({ conversation });
    if (before.turn !== 'running' || conversation.turn !== 'idle') return;
    // A turn that ended may have titled its conversation.
    void this.refreshConversations().catch(() => undefined);
    // A failed turn holds the queue: the next message would likely fail the same way.
    const [next] = this.value.queue;
    if (next && event.event !== 'chat.error' && !conversation.prompt) void this.sendNow(next.id);
  }

  /** The models a message can run on; the one picked stays while the server still offers it. */
  private async loadModels(): Promise<void> {
    try {
      const { names, active } = await this.options.sessions.models();
      const { model } = this.value;
      this.update({ models: names, model: model && names.includes(model) ? model : active });
    } catch {
      // The picker stays as it was; a message then runs on the conversation's model.
    }
  }

  private async refreshConversations(): Promise<readonly ResourceConversation[]> {
    const conversations = await this.options.sessions.conversations(this.options.documentId);
    this.update({ conversations });
    return conversations;
  }

  private async refreshDocuments(sessionId: SessionId): Promise<void> {
    try {
      const documents = await this.options.sessions.openDocuments(sessionId);
      if (sessionId === this.shown) this.update({ documents });
    } catch {
      // Names only: a task to another document then shows its fallback name.
    }
  }

  /** Change the transcript, unless the pane has moved on to another conversation meanwhile. */
  private updateShown(sessionId: SessionId, change: (conversation: Conversation) => Conversation): void {
    if (sessionId === this.shown) this.update({ conversation: change(this.value.conversation) });
  }

  private update(change: Partial<PaneState>): void {
    const before = this.value.conversation;
    this.value = { ...this.value, ...change };
    this.value.undo = { ...this.value.undo, offers: this.undoOffers() };
    this.value.retry = this.retryOffer();
    this.listeners.forEach(listener => listener(this.value));
    // A changes entry closes its turn: the turn's undo records what the turn left, then the entry offers it.
    const { snapshots } = this.options;
    if (snapshots && newCard(before, this.value.conversation)) void snapshots.closeTurn().then(() => this.update({}));
  }

  /**
   * A failure offers its retry while nothing came after it, so its turn wrote nothing a second run would write
   * again, and the turn was this pane's last message.
   */
  private retryOffer(): string | undefined {
    const { entries, turn } = this.value.conversation;
    const failure = entries.at(-1);
    const message = [...entries].reverse().find(entry => entry.kind === 'user');
    return failure?.kind === 'notice' && failure.failure?.action === 'retry' && turn === 'idle'
      && message !== undefined && message.id === this.lastMessage?.entryId ? failure.id : undefined;
  }

  /** A turn's undo while no turn runs and the pane holds the document. */
  private undoOffers(): ReadonlySet<string> {
    const { conversation, attachment } = this.value;
    const { snapshots } = this.options;
    if (!snapshots || conversation.turn === 'running' || attachment.kind !== 'attached') return new Set();
    return new Set(conversation.entries.flatMap(entry => (
      entry.kind === 'changes' && snapshots.undoable(entry.callIds) ? [entry.id] : [])));
  }
}

/** The shown conversation's turn runs or waits on the user, in this window: Stop stops it, and so does leaving it. */
export function busy({ attachment, conversation }: Pick<PaneState, 'attachment' | 'conversation'>): boolean {
  return attachment.kind === 'attached' && (conversation.turn === 'running' || conversation.prompt !== undefined);
}

/** Whether ``after`` goes on from ``before`` with a ``changes`` entry ``before`` did not have. */
function newCard(before: Conversation, after: Conversation): boolean {
  const last = after.entries.at(-1);
  return last?.kind === 'changes' && !before.entries.some(entry => entry.id === last.id);
}
