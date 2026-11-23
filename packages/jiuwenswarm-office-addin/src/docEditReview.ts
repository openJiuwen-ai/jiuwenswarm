/**
 * The suggestion cards of ``propose_doc_edits``: what became of each, kept in the document's own settings under
 * the call that proposed it and the id the model gave it, so the cards read the same when the pane opens again.
 * The pane is their only owner. The user applies a card (as an edit, as a tracked change or as a comment), skips
 * it or undoes it; the model withdraws its own pending ones. What the user decided reaches the conversation that
 * proposed the card with its next message (``<user_reviewed_edits>``), once per decision.
 */
import type { DocumentSettings } from './documentIdentity';
import { isRecord, type JsonObject } from './gatewayClient';
import type { HostWriteQueue } from './hostWriteQueue';
import type { ClientState } from './sessionFacade';
import { findPassage, Refused, replacePassage, type WordRun } from './wordPassage';
import { paragraphLines, unescapeText, wordHunks } from './wordText';

export const DOC_EDITS_SETTING = 'jiuwenswarm.doc_edits';

/** How the user applied a card. */
export type AppliedAs = 'edit' | 'tracked' | 'comment';

export type CardState =
  | { status: 'pending' | 'dismissed' | 'withdrawn' | 'undone' }
  /**
   * ``mark``: the hidden bookmark over the changed words, or the comment's id. ``words``, for an edit: the words it
   * took out as the document had them, and the words it put in.
   */
  | { status: 'applied'; as: AppliedAs; alternative?: number; mark: string; words?: { before: string; after: string } }
  | { status: 'failed'; error: string };

/** One ``propose_doc_edits`` call: its conversation, its cards, and the decision each last reported. */
interface Proposal {
  session: string;
  cards: Record<string, CardState>;
  reported: Record<string, string>;
}

/** One edit of a ``propose_doc_edits`` call, as the model gave it. */
export interface ProposedEdit {
  id: string;
  section: string;
  change_summary: string;
  old_text: string;
  new_text: string;
  new_text_label?: string;
  alternatives?: { label: string; text: string }[];
  context_before?: string;
  context_after?: string;
  reasoning?: string;
  comment?: { text: string; for?: string };
}

/** What a card shows: its state, whether the pane is acting on it now, and why the last action failed. */
export interface CardView {
  state: CardState;
  busy: boolean;
  error?: string;
}

const APPLIED_AS: Record<AppliedAs, string> = { edit: 'an edit', tracked: 'a tracked change', comment: 'a comment' };

export class DocEditReview {
  private readonly proposals: Record<string, Proposal>;
  private readonly busy = new Set<string>();
  private readonly errors = new Map<string, string>();
  private readonly listeners = new Set<() => void>();
  /** Moves on with every change a card shows. */
  version = 0;

  constructor(private readonly settings: DocumentSettings, private readonly run: WordRun,
    private readonly writes: HostWriteQueue) {
    const stored = settings.get(DOC_EDITS_SETTING);
    this.proposals = isRecord(stored) ? stored as unknown as Record<string, Proposal> : {};
  }

  subscribe(listener: () => void): void {
    this.listeners.add(listener);
  }

  /** The card of edit ``id`` of call ``callId``; undefined for an edit that never became a card. */
  card(callId: string, id: string): CardView | undefined {
    const state = this.proposals[callId]?.cards[id];
    if (!state) return undefined;
    const key = `${callId}\n${id}`;
    return { state, busy: this.busy.has(key), error: this.errors.get(key) };
  }

  /** A call delivered again (after a reload) keeps what became of its cards. */
  propose(callId: string, session: string, ids: readonly string[]): void {
    const proposal = this.proposals[callId] ??= { session, cards: {}, reported: {} };
    for (const id of ids) proposal.cards[id] ??= { status: 'pending' };
    this.changed();
  }

  /**
   * Withdraw the conversation's pending cards with these ids; what was not pending, or is being applied now, says
   * what it is.
   */
  withdraw(session: string, ids: readonly string[]): JsonObject {
    const withdrawn = new Set<string>();
    const kept = new Map<string, string>();
    for (const [callId, proposal] of Object.entries(this.proposals)) {
      if (proposal.session !== session) continue;
      for (const id of ids) {
        const state = proposal.cards[id];
        if (!state) continue;
        if (this.busy.has(`${callId}\n${id}`)) {
          kept.set(id, 'the user is applying it now');
        } else if (state.status === 'pending' || state.status === 'failed') {
          proposal.cards[id] = { status: 'withdrawn' };
          withdrawn.add(id);
        } else {
          kept.set(id, `the user already ${state.status} it`);
        }
      }
    }
    this.changed();
    const left = ids.filter(id => !withdrawn.has(id));
    return {
      withdrawn: [...withdrawn],
      ...(left.length ? { not_withdrawn: left.map(id => ({ id,
        reason: kept.get(id) ?? 'no suggestion of this conversation has this id' })) } : {}),
    };
  }

  dismiss(callId: string, id: string): void {
    this.settle(callId, id, { status: 'dismissed' });
  }

  /** Apply the card as the user chose; ``alternative`` 0 is ``new_text``, n is the n-th alternative. */
  apply(callId: string, edit: ProposedEdit, as: AppliedAs, alternative = 0): Promise<void> {
    if (this.proposals[callId]?.cards[edit.id]?.status !== 'pending') return Promise.resolve();
    return this.act(callId, edit.id, async context => {
      const passage = await findPassage(context, edit.old_text);
      if (as === 'comment') {
        const comment = passage.parts[0].expandTo(passage.parts.at(-1)!)
          .insertComment(edit.comment?.text ?? edit.reasoning ?? edit.change_summary);
        comment.load('id');
        await context.sync();
        return { status: 'applied', as, mark: comment.id };
      }
      const mark = bookmarkName(callId, edit.id);
      const text = textOf(edit, alternative);
      await tracking(context, as === 'tracked', async () => {
        const span = await replacePassage(context, passage, edit.old_text, text);
        if (!span) throw new Refused('The suggestion is the same as the document text; there is nothing to apply.');
        span.insertBookmark(mark);
        await context.sync();
      });
      const words = as === 'edit' ? { words: changedWords(passage.lines.join('\n'), edit.old_text, text) } : {};
      return { status: 'applied', as, mark, ...words, ...(alternative ? { alternative } : {}) };
    });
  }

  /**
   * Take back an applied card: reject its tracked changes, restore its words, or delete its comment. Words changed
   * since, or tracked changes no longer pending, are left as they are, with the reason on the card.
   */
  undo(callId: string, edit: ProposedEdit): Promise<void> {
    const state = this.proposals[callId]?.cards[edit.id];
    if (state?.status !== 'applied') return Promise.resolve();
    return this.act(callId, edit.id, async context => {
      if (state.as === 'comment') {
        const comments = context.document.body.getComments();
        comments.load('items/id');
        await context.sync();
        const comment = comments.items.find(item => item.id === state.mark);
        if (!comment) throw new Error('the comment is no longer in the document');
        comment.delete();
        await context.sync();
        return { status: 'undone' };
      }
      const range = context.document.getBookmarkRangeOrNullObject(state.mark);
      range.load('isNullObject,text');
      const changes = range.getTrackedChanges();
      changes.load('items');
      await context.sync();
      if (range.isNullObject) throw new Error('the changed words are no longer in the document');
      if (state.as === 'tracked') {
        if (!changes.items.length) throw new Error('its tracked changes were already accepted or rejected in Word');
        changes.rejectAll();
      } else {
        const { before, after } = state.words!;
        if (range.text.replace(/\r/g, '\n') !== after) {
          throw new Error('the changed words were edited since; change them back in the document');
        }
        await tracking(context, false, async () => {
          range.insertText(before, 'Replace');
          await context.sync();
        });
      }
      context.document.deleteBookmark(state.mark);
      await context.sync();
      return { status: 'undone' };
    }, state);
  }

  /**
   * The decisions on the conversation's cards not yet reported to it, as the next message carries them; ``sent``
   * marks them reported.
   */
  reviewed(session: string): ClientState {
    const decided: { proposal: Proposal; id: string; line: string }[] = [];
    for (const proposal of Object.values(this.proposals)) {
      if (proposal.session !== session) continue;
      for (const [id, state] of Object.entries(proposal.cards)) {
        const line = decision(state);
        if (line && proposal.reported[id] !== line) decided.push({ proposal, id, line });
      }
    }
    if (!decided.length) return { text: '', sent: () => undefined };
    return {
      text: `<user_reviewed_edits>\nEach line is one decision: the edit id as a quoted JSON string, then what the user `
        + `did. Suggestions not listed are still pending.\n${decided.map(({ id, line }) => `  ${JSON.stringify(id)}: ${
          line}`).join('\n')}\n</user_reviewed_edits>`,
      sent: () => {
        decided.forEach(({ proposal, id, line }) => { proposal.reported[id] = line; });
        this.save();
      },
    };
  }

  /**
   * Run one action on a card in the host write queue; a refusal or a host error fails a pending card and leaves
   * an applied one as it was, with the reason on the card.
   */
  private async act(callId: string, id: string, work: (context: Word.RequestContext) => Promise<CardState>,
    applied?: CardState): Promise<void> {
    const key = `${callId}\n${id}`;
    if (this.busy.has(key)) return;
    this.busy.add(key);
    this.errors.delete(key);
    this.changed(false);
    try {
      this.proposals[callId].cards[id] = await this.writes.exclusive(() => this.run(work));
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      if (applied) this.errors.set(key, reason);
      else this.proposals[callId].cards[id] = { status: 'failed', error: reason };
    } finally {
      this.busy.delete(key);
      this.changed();
    }
  }

  private settle(callId: string, id: string, state: CardState): void {
    this.proposals[callId].cards[id] = state;
    this.changed();
  }

  private changed(persist = true): void {
    this.version += 1;
    if (persist) this.save();
    this.listeners.forEach(listener => listener());
  }

  private save(): void {
    this.settings.set(DOC_EDITS_SETTING, this.proposals);
    this.settings.saveAsync(() => undefined);
  }
}

/**
 * The words an edit of ``oldText`` into ``text`` takes out, as ``documentText`` (the passage as the document has it,
 * its quotes included) holds them, and the words it puts in: from the first change to the end of the last.
 */
function changedWords(documentText: string, oldText: string, text: string): { before: string; after: string } {
  const hunks = wordHunks(paragraphLines(oldText).join('\n'), text);
  const [from, to] = [hunks[0].start, hunks.at(-1)!.end];
  let after = '';
  let at = from;
  for (const hunk of hunks) {
    after += documentText.slice(at, hunk.start) + hunk.text;
    at = hunk.end;
  }
  return { before: documentText.slice(from, to), after };
}

function textOf(edit: ProposedEdit, alternative: number): string {
  return unescapeText(alternative ? edit.alternatives![alternative - 1].text : edit.new_text);
}

/** A card's decision as ``<user_reviewed_edits>`` words it; none while it waits or after the model withdrew it. */
function decision(state: CardState): string | undefined {
  switch (state.status) {
    case 'applied':
      return `applied as ${APPLIED_AS[state.as]}${state.alternative ? ` (alternative ${state.alternative})` : ''}`;
    case 'failed':
      return `failed: ${state.error}`;
    case 'dismissed':
    case 'undone':
      return state.status;
    default:
      return undefined;
  }
}

/** Word hides a bookmark whose name starts with ``_``; names hold at most 40 letters, digits and underscores. */
function bookmarkName(callId: string, id: string): string {
  let hash = 2166136261;
  for (const char of `${callId}\n${id}`) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619) >>> 0;
  return `_jsw_edit_${hash.toString(36)}`;
}

/**
 * Run ``work`` with Track Changes on (``tracked``) or off, as the user chose for the card, and put back the
 * user's own setting afterwards.
 */
async function tracking(context: Word.RequestContext, tracked: boolean, work: () => Promise<void>): Promise<void> {
  const { document } = context;
  document.load('changeTrackingMode');
  await context.sync();
  const before = document.changeTrackingMode;
  const wanted = !tracked ? 'Off' : before === 'Off' ? 'TrackAll' : before;
  if (wanted !== before) document.changeTrackingMode = wanted;
  try {
    await work();
  } finally {
    if (wanted !== before) {
      document.changeTrackingMode = before;
      await context.sync();
    }
  }
}
