/**
 * Who changed the document: the pane's writes, in one critical section with a ledger of what they left, and the
 * user's changes not told yet.
 *
 * The critical section is first come first served.
 *
 * Tool writes and whatever must not interleave with them (an undo, a read of
 * what a write left) run one at a time, each in its turn. Reads stay outside.
 * Between two sections the queue yields a macrotask: a host whose calls are
 * synchronous (WPS) thereby lets the pane render between them.
 *
 * Once a host observes its places, the queue reads them before and after each section and keeps, by place, what the
 * section left there. A host reads all its places (Word's paragraphs) or only those a section touched (Excel's
 * ranges) and the few that tell its structure (Excel's sheets). A change the host reports at a place counts as the
 * user's unless the place still shows what the pane's last write left: one rule for every host, whenever and
 * however the host reports the change.
 */
import type { ClientState } from './sessionFacade';

export class HostWriteQueue {
  private tail: Promise<void> = Promise.resolve();
  /** By place, what the pane's last write left there. */
  private readonly left = new Map<Place, Look>();
  private looks?: (touched: readonly string[]) => Promise<Looks>;
  /** What the section under way wrote, as the host names it. */
  private touched: string[] = [];

  exclusive<T>(section: () => Promise<T>): Promise<T> {
    const run = this.tail.then(() => this.noted(section));
    this.tail = run.then(yieldMacrotask, yieldMacrotask);
    return run;
  }

  /** From the next section on, notes what each leaves at the places ``looks`` reads, given what it touched. */
  observe(looks: (touched: readonly string[]) => Promise<Looks>): void {
    this.looks = looks;
  }

  /** Notes that the section under way wrote at ``where``, in the host's terms (an Excel range's address). */
  touch(where: string): void {
    this.touched.push(where);
  }

  /** What the pane's last write left at ``place``; undefined where no write of the pane changed it. */
  leftAt(place: Place): Look | undefined {
    return this.left.get(place);
  }

  /** Settles once the sections queued so far have ended, and with them their entries in the ledger. */
  idle(): Promise<void> {
    return this.tail;
  }

  private async noted<T>(section: () => Promise<T>): Promise<T> {
    const { looks } = this;
    if (!looks) return section();
    // A document that cannot be read just now leaves this section out of the ledger.
    const read = (touched: readonly string[]) => looks(touched).catch(() => undefined);
    this.touched = [];
    const before = await read([]);
    try {
      return await section();
    } finally {
      const after = await read(this.touched);
      if (before && after) {
        after.forEach((now, place) => before.get(place) !== now && this.left.set(place, now));
        before.forEach((_was, place) => !after.has(place) && this.left.set(place, null));
      }
    }
  }
}

/** Where a write can leave something, in the host's terms: a Word paragraph's id, an Excel sheet's id or a cell. */
export type Place = string;
/** What a place shows, as the host compares it; null where there is nothing (deleted, or an empty cell). */
export type Look = string | null;
/** Places and what they show; a place read only after a section counts as changed by it. */
export type Looks = Map<Place, Look>;

function yieldMacrotask(): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, 0));
}

/** Changes the next message lists at most, the latest kept. */
const MAX_CHANGES = 100;

/**
 * The user's changes not listed yet, as the ``<user_changes>`` block lists them, one line each in the order last
 * recorded. A change recorded under the key of one still pending replaces it; the oldest past the limit is dropped
 * and counted.
 */
export class UserChanges<T> {
  private readonly pending = new Map<string, T>();
  private omitted = 0;
  private added = 0;

  constructor(private readonly line: (change: T) => string) {}

  get(key: string): T | undefined {
    return this.pending.get(key);
  }

  /** Records ``change`` under ``key`` as the latest; undefined drops the key's change. */
  set(key: string, change: T | undefined): void {
    this.pending.delete(key);
    if (change === undefined) return;
    this.pending.set(key, change);
    if (this.pending.size > MAX_CHANGES) {
      this.pending.delete(this.pending.keys().next().value!);
      this.omitted += 1;
    }
  }

  /** Records ``change`` as one of its own, never replaced. */
  add(change: T): void {
    this.added += 1;
    this.set(`#${this.added}`, change);
  }

  /** The block the next message carries, empty when there is nothing; once sent, what it lists is not listed again. */
  block(): ClientState {
    const listed = [...this.pending].map(([key, change]) => [key, this.line(change)] as const);
    const { omitted } = this;
    const lines = listed.map(([, line]) => line);
    if (omitted) lines.push(`[${omitted} older change${omitted === 1 ? ' is' : 's are'} not listed.]`);
    return { text: lines.length ? `<user_changes>\n${lines.join('\n')}\n</user_changes>` : '', sent: () => {
      // A change recorded again since keeps its newer line.
      for (const [key, line] of listed) {
        const change = this.pending.get(key);
        if (change !== undefined && this.line(change) === line) this.pending.delete(key);
      }
      this.omitted -= omitted;
    } };
  }
}
