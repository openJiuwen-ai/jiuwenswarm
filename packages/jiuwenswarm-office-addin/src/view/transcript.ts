import type { Entry } from '../conversationModel';
import { copy, type DocumentCopy } from '../copy';
import type { UndoState } from '../paneController';
import { activityGroup, running, StepClock, tickSeconds, type Step } from './activity';
import { h, syncChildren } from './dom';
import { writeClipboard } from './clipboard';
import { icon, type IconName } from './icons';
import { renderMarkdown } from './markdown';

/** How close to the end a reader must be for the transcript to follow new text. */
const FOLLOW_THRESHOLD_PX = 72;
/** Relative to the page, which the desktop serves under `/office/`. */
const BEE = 'assets/bee.png';
/** How long Copy shows that it copied. */
const COPIED_MS = 2000;

/** What a finished turn offers under its reply: the reply to copy, and the turn's changes to undo. */
interface TurnEnd { reply: string; changes?: string }

/**
 * A transcript item: one entry, a run of steps shown as one timeline, the
 * actions under a finished turn, or the dots while the agent works unseen.
 */
type Item =
  | { id: string; entry: Exclude<Entry, Step | { kind: 'changes' }> }
  | { id: string; steps: Step[] }
  | { id: string; end: TurnEnd }
  | { id: string; thinking: true };

function isStep(entry: Entry): entry is Step {
  return entry.kind === 'tool' || entry.kind === 'outgoing';
}

/** A turn starts at a message to the agent; it ends at the next one, or when the agent is done. */
function items(entries: readonly Entry[], working: boolean, openReply: string | undefined): Item[] {
  const result: Item[] = [];
  let turn = { id: 'start', end: { reply: '' } as TurnEnd };
  const close = (): void => {
    if (turn.end.reply || turn.end.changes) result.push({ id: `end:${turn.id}`, end: turn.end });
  };
  for (const entry of entries) {
    if (entry.kind === 'user' || entry.kind === 'incoming') {
      close();
      turn = { id: entry.id, end: { reply: '' } };
    }
    if (entry.kind === 'changes') {
      turn.end.changes = entry.id;
      continue;
    }
    if (entry.kind === 'assistant' && entry.text) {
      turn.end.reply = turn.end.reply ? `${turn.end.reply}\n\n${entry.text}` : entry.text;
    }
    const last = result.at(-1);
    if (!isStep(entry)) result.push({ id: entry.id, entry });
    else if (last && 'steps' in last) last.steps.push(entry);
    else result.push({ id: entry.id, steps: [entry] });
  }
  if (!working) close();
  else if (unseen(entries.at(-1), openReply)) result.push({ id: 'thinking', thinking: true });
  return result;
}

/** Nothing on screen moves while the agent works: no step runs and no reply streams. */
function unseen(last: Entry | undefined, openReply: string | undefined): boolean {
  if (last && isStep(last)) return !running(last);
  return !(last?.kind === 'assistant' && last.id === openReply && last.text);
}

export interface TranscriptState {
  entries: readonly Entry[];
  documents: ReadonlyMap<string, string>;
  /** The reply that streams now. */
  openReply?: string;
  /** The agent works on a turn and waits for no answer. */
  working: boolean;
  loadingHistory: boolean;
  /** Whether a suggestion of the empty conversation can go to the composer. */
  canSuggest: boolean;
  undo: UndoState;
}

/** The user's actions on a turn's undo. */
export interface UndoActions {
  undo: (card: string, overwrite?: boolean) => void;
  cancelUndo: () => void;
}

/**
 * The conversation. An item is rebuilt only when what it shows changed, so a
 * streaming reply redraws that reply alone. It is not a live region: a
 * streamed reply would be read out again on every delta, so the pane announces
 * through its own region instead.
 */
export class Transcript {
  readonly element = h('div', { class: 'transcript', role: 'region', 'aria-label': copy.transcript, tabindex: '0' });
  private readonly clock = new StepClock();
  private rendered = new Map<string, { signature: unknown[]; node: HTMLElement }>();
  /** Groups the user opened; the others stay one line. */
  private readonly groupOverride = new Map<string, boolean>();
  private openStep: string | undefined;
  /** The turn whose reply was just copied. */
  private copied: string | undefined;
  private copiedTimer: ReturnType<typeof setTimeout> | undefined;
  private last: TranscriptState | undefined;
  private readonly loading = h('p', { class: 'transcript-loading' }, copy.loadingHistory);
  private readonly suggestions: HTMLButtonElement[];
  private readonly welcomeNode: HTMLElement;

  constructor(words: DocumentCopy, suggest: (text: string) => void, private readonly actions: UndoActions) {
    this.suggestions = words.suggestions.map(text => h('button', {
      type: 'button', class: 'suggestion', onclick: () => suggest(text),
    }, text));
    this.welcomeNode = h('div', { class: 'welcome' },
      h('img', { class: 'welcome-mascot', src: BEE, alt: '', width: '80', height: '80' }),
      h('h2', { class: 'welcome-title' }, copy.welcomeTitle),
      h('p', { class: 'welcome-body' }, words.welcomeBody),
      h('div', { class: 'suggestions' }, ...this.suggestions));
  }

  update(state: TranscriptState): void {
    this.last = state;
    const { element } = this;
    const following = element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_THRESHOLD_PX;
    const focusKey = document.activeElement?.closest('[data-key]')?.getAttribute('data-key');
    this.clock.note(state.entries.filter(isStep), Date.now());
    const next = new Map<string, { signature: unknown[]; node: HTMLElement }>();
    const nodes = items(state.entries, state.working, state.openReply).map(item => {
      const signature = this.signature(item, state);
      const before = this.rendered.get(item.id);
      const node = before && sameSignature(before.signature, signature) ? before.node : this.itemNode(item, state);
      next.set(item.id, { signature, node });
      return node;
    });
    this.rendered = next;
    if (!nodes.length) nodes.push(state.loadingHistory ? this.loading : this.welcome(state.canSuggest));
    syncChildren(element, nodes);
    if (focusKey && !element.contains(document.activeElement)) {
      element.querySelector<HTMLElement>(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
    }
    if (following) element.scrollTop = element.scrollHeight;
  }

  /** The running steps' seconds move on. */
  tick(now: number): void {
    tickSeconds(this.element, now);
  }

  private rerender(): void {
    if (this.last) this.update(this.last);
  }

  private signature(item: Item, state: TranscriptState): unknown[] {
    if ('entry' in item) return [item.entry, state.openReply === item.id];
    if ('thinking' in item) return [];
    if ('end' in item) {
      const { reply, changes } = item.end;
      const { offers, confirm } = state.undo;
      return [reply, changes, changes !== undefined && offers.has(changes), changes !== undefined && confirm === changes,
        this.copied === item.id];
    }
    return [...item.steps, state.documents, this.groupOverride.get(item.id),
      item.steps.some(step => step.id === this.openStep) ? this.openStep : null];
  }

  private itemNode(item: Item, state: TranscriptState): HTMLElement {
    if ('steps' in item) {
      return activityGroup(item.id, item.steps, {
        documents: state.documents,
        clock: this.clock,
        expanded: this.groupOverride.get(item.id) ?? false,
        openStep: this.openStep,
        toggleGroup: () => {
          this.groupOverride.set(item.id, !this.groupOverride.get(item.id));
          this.rerender();
        },
        toggleStep: id => {
          this.openStep = this.openStep === id ? undefined : id;
          this.rerender();
        },
      });
    }
    if ('thinking' in item) {
      return h('div', { class: 'entry thinking', role: 'img', 'aria-label': copy.thinking }, h('span'), h('span'), h('span'));
    }
    if ('end' in item) {
      return turnActions(item.id, item.end, state.undo, this.copied === item.id, {
        ...this.actions, copy: () => void this.copy(item.id, item.end.reply),
      });
    }
    return entryNode(item.entry, state.openReply === item.id);
  }

  private async copy(id: string, text: string): Promise<void> {
    if (!await writeClipboard(text)) return;
    clearTimeout(this.copiedTimer);
    this.copied = id;
    this.rerender();
    this.copiedTimer = setTimeout(() => {
      this.copied = undefined;
      this.rerender();
    }, COPIED_MS);
  }

  private welcome(canSuggest: boolean): HTMLElement {
    this.suggestions.forEach(button => {
      button.disabled = !canSuggest;
    });
    return this.welcomeNode;
  }
}

function sameSignature(a: unknown[], b: unknown[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/**
 * The icons under a finished turn: copy its reply, and undo its changes while
 * offered. Undo asks in one line first when it would overwrite later changes.
 */
function turnActions(
  id: string, end: TurnEnd, undo: UndoState, copied: boolean, actions: UndoActions & { copy: () => void },
): HTMLElement {
  const { changes } = end;
  if (changes !== undefined && undo.confirm === changes) {
    return h('div', { class: 'entry turn-actions', role: 'alert' },
      h('span', { class: 'undo-ask' }, copy.undoChanged),
      h('button', { type: 'button', class: 'undo-button', 'data-key': `undo:${changes}`, onclick: actions.cancelUndo }, copy.cancel),
      h('button', { type: 'button', class: 'undo-button', onclick: () => actions.undo(changes, true) }, copy.undoAnyway));
  }
  return h('div', { class: 'entry turn-actions' },
    end.reply ? actionButton(copied ? 'check' : 'copy', copied ? copy.copied : copy.copyReply, `copy:${id}`, actions.copy) : null,
    changes !== undefined && undo.offers.has(changes)
      ? actionButton('undo', copy.undo, `undo:${changes}`, () => actions.undo(changes)) : null);
}

function actionButton(name: IconName, label: string, key: string, onclick: () => void): HTMLButtonElement {
  return h('button', {
    type: 'button', class: 'icon-button action-button', 'aria-label': label, title: label, 'data-key': key, onclick,
  }, icon(name));
}

/** One transcript entry that is not a step. */
function entryNode(entry: Exclude<Entry, Step | { kind: 'changes' }>, streaming: boolean): HTMLElement {
  switch (entry.kind) {
    case 'user':
      return h('div', { class: 'entry entry-user' }, h('div', { class: 'bubble' }, entry.text));
    case 'assistant':
      return h('div', { class: `entry entry-assistant${streaming ? ' is-streaming' : ''}` },
        h('div', { class: 'reply' }, ...renderMarkdown(entry.text)));
    case 'incoming':
      return h('section', { class: 'entry entry-incoming' },
        h('div', { class: 'incoming-head' }, icon('incoming'),
          h('span', { class: 'incoming-title' }, copy.incomingFrom(entry.sourceTitle || copy.otherConversation)),
          entry.status ? h('span', { class: 'chip' }, copy.messageStatus[entry.status] ?? entry.status) : null),
        h('div', { class: 'incoming-body' }, ...renderMarkdown(entry.text)));
    case 'notice':
      return h('p', { class: `entry entry-notice tone-${entry.tone}` },
        entry.tone === 'error' ? icon('alert') : null, h('span', {}, entry.text));
  }
}
