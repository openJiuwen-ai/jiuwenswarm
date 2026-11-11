import type { Edit, Entry } from '../conversationModel';
import { copy, toolLabel } from '../copy';
import { activityGroup, running, StepClock, tickSeconds, type Step } from './activity';
import { h, syncChildren } from './dom';
import { icon } from './icons';
import { renderMarkdown } from './markdown';

/** How close to the end a reader must be for the transcript to follow new text. */
const FOLLOW_THRESHOLD_PX = 72;
const BEE = '/assets/bee.png';

/** A transcript item: one entry, or a run of steps shown as one timeline. */
type Item = { id: string; entry: Exclude<Entry, Step> } | { id: string; steps: Step[] };

function isStep(entry: Entry): entry is Step {
  return entry.kind === 'tool' || entry.kind === 'outgoing';
}

function items(entries: readonly Entry[]): Item[] {
  const result: Item[] = [];
  for (const entry of entries) {
    const last = result.at(-1);
    if (!isStep(entry)) result.push({ id: entry.id, entry });
    else if (last && 'steps' in last) last.steps.push(entry);
    else result.push({ id: entry.id, steps: [entry] });
  }
  return result;
}

export interface TranscriptState {
  entries: readonly Entry[];
  documents: ReadonlyMap<string, string>;
  /** The reply that streams now. */
  openReply?: string;
  loadingHistory: boolean;
  /** Whether a suggestion of the empty conversation can go to the composer. */
  canSuggest: boolean;
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
  /** Groups the user opened or closed; the others are open while they run. */
  private readonly groupOverride = new Map<string, boolean>();
  private openStep: string | undefined;
  private last: TranscriptState | undefined;
  private readonly loading = h('p', { class: 'transcript-loading' }, copy.loadingHistory);
  private readonly suggestions = copy.suggestions.map(text => h('button', {
    type: 'button', class: 'suggestion', onclick: () => this.suggest(text),
  }, text));
  private readonly welcomeNode = h('div', { class: 'welcome' },
    h('img', { class: 'welcome-mascot', src: BEE, alt: '', width: '80', height: '80' }),
    h('h2', { class: 'welcome-title' }, copy.welcomeTitle),
    h('p', { class: 'welcome-body' }, copy.welcomeBody),
    h('div', { class: 'suggestions' }, ...this.suggestions));

  constructor(private readonly suggest: (text: string) => void) {}

  update(state: TranscriptState): void {
    this.last = state;
    const { element } = this;
    const following = element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOW_THRESHOLD_PX;
    const focusKey = document.activeElement?.closest('[data-key]')?.getAttribute('data-key');
    this.clock.note(state.entries.filter(isStep), Date.now());
    const next = new Map<string, { signature: unknown[]; node: HTMLElement }>();
    const nodes = items(state.entries).map(item => {
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
    if ('entry' in item) {
      return [item.entry, state.openReply === item.id];
    }
    return [...item.steps, state.documents, this.groupOverride.get(item.id),
      item.steps.some(step => step.id === this.openStep) ? this.openStep : null];
  }

  private itemNode(item: Item, state: TranscriptState): HTMLElement {
    if ('steps' in item) {
      return activityGroup(item.id, item.steps, {
        documents: state.documents,
        clock: this.clock,
        expanded: this.groupOverride.get(item.id) ?? item.steps.some(running),
        openStep: this.openStep,
        toggleGroup: () => {
          const expanded = this.groupOverride.get(item.id) ?? item.steps.some(running);
          this.groupOverride.set(item.id, !expanded);
          this.rerender();
        },
        toggleStep: id => {
          this.openStep = this.openStep === id ? undefined : id;
          this.rerender();
        },
      });
    }
    return entryNode(item.entry, state.openReply === item.id);
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

/** One transcript entry that is not a step. */
function entryNode(entry: Exclude<Entry, Step>, streaming: boolean): HTMLElement {
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
    case 'changes':
      return h('section', { class: 'entry entry-changes', 'aria-label': copy.changesTitle },
        h('h3', { class: 'changes-title' }, icon('pen'), copy.changesTitle),
        h('ul', { class: 'changes-list' }, ...changeRows(entry.edits).map(({ name, failed, count }) => h('li', {
          class: failed ? 'is-failed' : '',
        }, icon(failed ? 'close' : 'check'), h('span', { class: 'changes-name' }, toolLabel(name)),
        count > 1 ? h('span', { class: 'changes-count' }, copy.times(count)) : null,
        failed ? h('span', { class: 'chip tone-danger' }, copy.changesFailed) : null))));
  }
}

/** The turn's edits, one row per tool and outcome, in the order they first ran. */
function changeRows(edits: readonly Edit[]): { name: string; failed: boolean; count: number }[] {
  const rows = new Map<string, { name: string; failed: boolean; count: number }>();
  for (const { name, failed } of edits) {
    const key = `${name}:${failed}`;
    const row = rows.get(key) ?? { name, failed, count: 0 };
    row.count += 1;
    rows.set(key, row);
  }
  return [...rows.values()];
}
