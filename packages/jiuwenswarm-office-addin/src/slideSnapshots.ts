/**
 * Per-turn snapshots of the slides a turn's writes change, and "undo this turn".
 *
 * PowerPoint folds an add-in's writes into one undo step of its own, so the
 * pane keeps what it needs to undo a turn: the deck's slide order at the
 * turn's first write, and an export of each existing slide from before the
 * turn first changed it. A slide that is only moved needs no export; a new one
 * needs only its id. At the end of the turn the pane hashes the slides the turn
 * left, so an undo can tell whether one was changed since. The hash leaves out
 * ``docProps/core.xml``: PowerPoint writes the time of the export into it.
 *
 * Undoing turn T undoes T and every later turn, the last first. It runs in the
 * host write queue, so no tool write lands half way through. A call the host
 * failed part way is rolled back on its own, from exports taken before it. The snapshots live
 * in the pane's memory only: a reload loses them, and the pane keeps the last
 * ``turns`` turns or ``bytes`` of exports, whichever is less.
 */
import { strToU8, unzipSync } from 'fflate';

import type { HostWriteQueue } from './hostWriteQueue';
import { sha256Hex } from './presentationAssetClient';
import type { SlideJournal } from './presentationScenePatch';

/** The deck as an undo works it, one host round trip per call. */
export interface SlideDeck {
  ids(): Promise<string[]>;
  /** The exports of those of ``ids`` still in the deck. */
  exportSlides(ids: readonly string[]): Promise<Map<string, string>>;
  /** Insert an exported slide; its new id. */
  insert(exported: string): Promise<string>;
  remove(ids: readonly string[]): Promise<void>;
  /** Put every slide of the deck in this order. */
  order(ids: readonly string[]): Promise<void>;
}

/** What turns the pane keeps for undo. */
export interface SnapshotLimits { turns: number; bytes: number }

export type UndoResult =
  /** Slides the turns left were changed since; nothing was undone. */
  | { kind: 'changed'; slides: number }
  /** ``positions``: where the slides put back stand now, 1-based. Their ids are new. */
  | { kind: 'undone'; turns: number; positions: number[] }
  /** The pane no longer holds the turn. */
  | { kind: 'gone' };

/** What putting the deck back needs: its slide order then, the slides changed since as they were, the ones made since. */
interface Restorable {
  order: string[];
  exports: Map<string, string>;
  created: Set<string>;
}

/** One turn's record, every slide by its id now. */
interface Turn extends Restorable {
  callIds: Set<string>;
  /** The deck's slides at the turn's first write. */
  order: string[];
  /** Existing slides the turn changed or deleted, as they were before it. */
  exports: Map<string, string>;
  moved: Set<string>;
  created: Set<string>;
  /** Set when the turn ended: the hash of each slide it left with content of its own making. */
  after?: Map<string, string>;
}

const DEFAULT_LIMITS: SnapshotLimits = { turns: 10, bytes: 64 * 1024 * 1024 };

export class SlideSnapshots {
  /** Oldest first; only the last can be open (no ``after``). */
  private turns: Turn[] = [];
  /** Set by ``forget`` until the turn under way ends, whose later writes are not recorded either. */
  private forgotten = false;

  constructor(
    private readonly deck: SlideDeck,
    private readonly writes: HostWriteQueue,
    private readonly limits: SnapshotLimits = DEFAULT_LIMITS,
  ) {}

  /**
   * What the call ``callId`` tells the open turn about the slides it changes; used inside its write. Its
   * ``rollback`` names each slide put back by the id the call was given, not by a copy's.
   */
  journal(callId: string): SlideJournal {
    const call: Restorable = { order: [], exports: new Map(), created: new Set() };
    /** What the call added to its turn's record, taken out again when the call is rolled back. */
    const added = { exports: new Set<string>(), moved: new Set<string>() };
    /** The id the call was given for each copy it made of a slide (``replaced``), which it alone has seen. */
    const origin = new Map<string, string>();
    return {
      before: async (order, slides, exportSlides) => {
        const turn = this.openTurn(callId, order);
        call.order = [...order];
        const changed = slides.filter(slide => slide.content);
        const exported = await exportSlides(changed.map(slide => slide.id));
        changed.forEach(({ id }, at) => {
          call.exports.set(id, exported[at]);
          if (turn.created.has(id) || turn.exports.has(id)) return;
          turn.exports.set(id, exported[at]);
          added.exports.add(id);
        });
        for (const { id, content } of slides) {
          if (content || turn.created.has(id) || turn.moved.has(id)) continue;
          turn.moved.add(id);
          added.moved.add(id);
        }
        this.evict();
      },
      created: id => {
        call.created.add(id);
        this.openTurn(callId, []).created.add(id);
      },
      rollback: async () => {
        const renamed = await this.restore(call);
        const turn = this.turns.at(-1);
        if (turn) {
          added.exports.forEach(id => turn.exports.delete(id));
          added.moved.forEach(id => turn.moved.delete(id));
          call.created.forEach(id => turn.created.delete(id));
        }
        await this.rename(renamed);
        return new Map([...renamed].map(([id, restored]) => [origin.get(id) ?? id, restored]));
      },
      replaced: (oldId, newId) => {
        origin.set(newId, origin.get(oldId) ?? oldId);
        origin.delete(oldId);
        const renamed = new Map([[oldId, newId]]);
        for (const record of [call, ...this.turns]) {
          record.created.delete(newId);
          renameIds(record, renamed);
        }
        added.exports = new Set([...added.exports].map(id => renamed.get(id) ?? id));
        added.moved = new Set([...added.moved].map(id => renamed.get(id) ?? id));
      },
    };
  }

  /**
   * Nothing up to the end of the turn under way can be undone: a write changed slides no turn recorded (an edit of
   * the slide master moves every slide of that master to the new one), so putting back older exports would mix the
   * two masters.
   */
  forget(): void {
    this.turns = [];
    this.forgotten = true;
  }

  /** The turn is over: hash what it left. Queued behind its writes. */
  closeTurn(): Promise<void> {
    this.forgotten = false;
    const turn = this.turns.at(-1);
    if (!turn || turn.after) return Promise.resolve();
    return this.writes.exclusive(async () => {
      turn.after = await this.hashes([...turn.exports.keys(), ...turn.created]);
      this.evict();
    });
  }

  /** How many slides undoing the ended turn of these calls puts back, that turn and the later ones together. */
  undoable(callIds: readonly string[]): { slides: number } | undefined {
    const from = this.turnOf(callIds);
    return from < 0 ? undefined : { slides: new Set(this.turns.slice(from).flatMap(touched)).size };
  }

  /** Undo the ended turn of these calls and every later one; asks first (``changed``) unless ``overwrite``. */
  undo(callIds: readonly string[], overwrite = false): Promise<UndoResult> {
    return this.writes.exclusive(async () => {
      const from = this.turnOf(callIds);
      if (from < 0) return { kind: 'gone' };
      const undone = this.turns.slice(from);
      if (!overwrite) {
        const changed = await this.changedSince(undone);
        if (changed) return { kind: 'changed', slides: changed };
      }
      let restored = new Set<string>();
      for (const turn of undone.reverse()) {
        const renamed = await this.restore(turn);
        this.turns.pop();
        restored = new Set([...restored].map(id => renamed.get(id) ?? id));
        [...renamed.values(), ...turn.moved].forEach(id => restored.add(id));
        await this.rename(renamed);
      }
      const ids = await this.deck.ids();
      return { kind: 'undone', turns: undone.length,
        positions: ids.flatMap((id, at) => (restored.has(id) ? [at + 1] : [])) };
    });
  }

  private openTurn(callId: string, order: readonly string[]): Turn {
    let turn = this.turns.at(-1);
    if (!turn || turn.after || this.forgotten) {
      turn = { callIds: new Set(), order: [...order], exports: new Map(), moved: new Set(), created: new Set() };
      if (!this.forgotten) this.turns.push(turn);
    }
    turn.callIds.add(callId);
    return turn;
  }

  /** The ended turn one of the calls wrote in, or -1. */
  private turnOf(callIds: readonly string[]): number {
    return this.turns.findIndex(turn => turn.after && callIds.some(id => turn.callIds.has(id)));
  }

  /**
   * How many slides are no longer as the last turn that wrote them left them; a slide that turn left out of the
   * deck (deleted) should still be out.
   */
  private async changedSince(turns: readonly Turn[]): Promise<number> {
    const expected = new Map(turns.flatMap(turn => [...turn.exports.keys(), ...turn.created]
      .map(id => [id, turn.after!.get(id)] as const)));
    const now = await this.hashes([...expected.keys()]);
    return [...expected].filter(([id, hash]) => now.get(id) !== hash).length;
  }

  /** Put the deck back as it was before ``turn``; the new id of each slide it put back. */
  private async restore(turn: Restorable): Promise<Map<string, string>> {
    const renamed = new Map<string, string>();
    // Insert before removing, so the deck never runs out of slides.
    for (const [id, exported] of turn.exports) renamed.set(id, await this.deck.insert(exported));
    const present = new Set(await this.deck.ids());
    await this.deck.remove([...turn.created, ...turn.exports.keys()].filter(id => present.has(id)));
    const ids = await this.deck.ids();
    const order = turn.order.map(id => renamed.get(id) ?? id).filter(id => ids.includes(id));
    // A slide the user added since keeps its place after the slide it follows now.
    ids.forEach((id, at) => {
      if (!order.includes(id)) order.splice(at ? order.indexOf(ids[at - 1]) + 1 : 0, 0, id);
    });
    await this.deck.order(order);
    return renamed;
  }

  /** The turns left refer to the slides an undo put back by their new ids, which now look as those turns left them. */
  private async rename(renamed: Map<string, string>): Promise<void> {
    if (!renamed.size) return;
    for (const turn of this.turns) renameIds(turn, renamed);
    const fresh = await this.hashes([...renamed.values()]);
    for (const turn of this.turns) {
      for (const [id, hash] of fresh) if (turn.after?.has(id)) turn.after.set(id, hash);
    }
  }

  private async hashes(ids: readonly string[]): Promise<Map<string, string>> {
    const exports = await this.deck.exportSlides(ids);
    return new Map(await Promise.all([...exports].map(async ([id, exported]) => (
      [id, await contentHash(exported)] as const))));
  }

  /** Drop the oldest turns past the limits; the last turn stays, open or not. */
  private evict(): void {
    const bytes = () => this.turns.reduce((sum, turn) => sum + [...turn.exports.values()]
      .reduce((size, exported) => size + exported.length, 0), 0);
    while (this.turns.length > 1 && (this.turns.length > this.limits.turns || bytes() > this.limits.bytes)) {
      this.turns.shift();
    }
  }
}

/** The hash of an exported slide's parts, without ``docProps/core.xml`` and the zip's own times. */
async function contentHash(exported: string): Promise<string> {
  const parts = unzipSync(Uint8Array.from(atob(exported), char => char.charCodeAt(0)),
    { filter: file => file.name !== 'docProps/core.xml' });
  const digests = await Promise.all(Object.keys(parts).sort()
    .map(async name => `${name} ${await sha256Hex(new Uint8Array(parts[name]))}`));
  return sha256Hex(strToU8(digests.join('\n')));
}

/** The record names each slide of ``renamed`` by its new id. */
function renameIds(record: Restorable & { moved?: Set<string>; after?: Map<string, string> },
  renamed: Map<string, string>): void {
  const to = (id: string) => renamed.get(id) ?? id;
  record.order = record.order.map(to);
  record.exports = new Map([...record.exports].map(([id, exported]) => [to(id), exported]));
  record.created = new Set([...record.created].map(to));
  if (record.moved) record.moved = new Set([...record.moved].map(to));
  if (record.after) record.after = new Map([...record.after].map(([id, hash]) => [to(id), hash]));
}

/** The slides an undo of the turn puts back. */
function touched(turn: Turn): string[] {
  return [...turn.exports.keys(), ...turn.moved, ...turn.created];
}
