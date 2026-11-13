import { describe, expect, it } from 'vitest';

import { FakeDeck } from './fakePowerPoint';
import { HostWriteQueue } from './hostWriteQueue';
import { powerPointSlideDeck } from './powerPointWriteTools';
import { SlideSnapshots } from './slideSnapshots';

const MB = 1024 * 1024;

function setUp(slides = 3, limits?: { turns: number; bytes: number }) {
  const deck = new FakeDeck({ slides: Array.from({ length: slides }, (_, at) => ({
    shapes: [{ text: `slide ${at + 1}` }] })) });
  const snapshots = new SlideSnapshots(powerPointSlideDeck(deck.run), new HostWriteQueue(), limits);
  const exportSlides = async (ids: string[]) => ids.map(id => Buffer.from(JSON.stringify(deck.slide(id))).toString('base64'));
  /** A write of call ``callId``: tells the journal what it changes, then changes it. */
  const write = async (callId: string, change: {
    edit?: string[]; move?: [string, number][]; remove?: string[]; add?: number;
  }) => {
    const journal = snapshots.journal(callId);
    const touched = [
      ...(change.edit ?? []).map(id => ({ id, content: true })),
      ...(change.remove ?? []).map(id => ({ id, content: true })),
      ...(change.move ?? []).map(([id]) => ({ id, content: false })),
    ];
    await journal.before(deck.ids(), touched, exportSlides);
    for (const id of change.edit ?? []) deck.slide(id).shapes[0].text += ` (${callId})`;
    for (const [id, index] of change.move ?? []) {
      const slide = deck.slide(id);
      deck.slides.splice(deck.slides.indexOf(slide), 1);
      deck.slides.splice(index, 0, slide);
    }
    deck.slides = deck.slides.filter(slide => !change.remove?.includes(slide.id));
    for (let at = 0; at < (change.add ?? 0); at += 1) {
      const id = `${900 + deck.slides.length}#`;
      deck.slides.push({ id, shapes: [{ id: '1', name: 'n', type: 'TextBox', left: 0, top: 0, width: 1, height: 1,
        text: `new by ${callId}` }] });
      journal.created(id);
    }
  };
  return { deck, snapshots, write };
}

describe('undoing a turn', () => {
  it('puts back what the turn changed: new slides go, deleted ones return where they were, moved ones move back', async () => {
    const { deck, snapshots, write } = setUp(4);
    const [a, b, c, d] = deck.ids();
    const before = deck.content();
    await write('c1', { edit: [b], add: 2 });
    await write('c2', { remove: [c], move: [[d, 0]] });
    await write('c3', { edit: [b] });
    await snapshots.closeTurn();

    expect(snapshots.undoable(['c2'])).toEqual({ slides: 5 });
    const undone = await snapshots.undo(['c2']);
    expect(deck.content()).toEqual(before);
    expect(deck.ids()).toContain(a);
    expect(deck.ids()).toContain(d);
    expect(undone).toEqual({ kind: 'undone', turns: 1, positions: [2, 3, 4] });
    expect(snapshots.undoable(['c1'])).toBeUndefined();
  });

  it('keeps a turn undoable only once it has ended', async () => {
    const { deck, snapshots, write } = setUp();
    await write('c1', { edit: [deck.ids()[0]] });
    expect(snapshots.undoable(['c1'])).toBeUndefined();
    await snapshots.closeTurn();
    expect(snapshots.undoable(['c1'])).toEqual({ slides: 1 });
    expect(snapshots.undoable(['other'])).toBeUndefined();
  });

  it('undoes the later turns with it, the last first, following the ids an earlier undo gave', async () => {
    const { deck, snapshots, write } = setUp();
    const [a, b] = deck.ids();
    const before = deck.content();
    await write('t1', { edit: [a], add: 1 });
    await snapshots.closeTurn();
    const afterFirst = deck.content();
    const made = deck.ids()[3];
    await write('t2', { edit: [a, made], move: [[b, 0]] });
    await snapshots.closeTurn();

    expect(snapshots.undoable(['t1'])).toEqual({ slides: 3 });
    await snapshots.undo(['t2']);
    expect(deck.content()).toEqual(afterFirst);
    expect(await snapshots.undo(['t1'])).toMatchObject({ kind: 'undone', turns: 1 });
    expect(deck.content()).toEqual(before);
  });

  it('asks before it overwrites a slide that changed since the turn, and overwrites when told to', async () => {
    const { deck, snapshots, write } = setUp();
    const [a] = deck.ids();
    const before = deck.content();
    await write('c1', { edit: [a] });
    await snapshots.closeTurn();
    deck.slide(a).shapes[0].text = 'typed by the user';

    expect(await snapshots.undo(['c1'])).toEqual({ kind: 'changed', slides: 1 });
    expect(deck.slide(a).shapes[0].text).toBe('typed by the user');
    expect(await snapshots.undo(['c1'], true)).toMatchObject({ kind: 'undone' });
    expect(deck.content()).toEqual(before);
  });

  it('counts a slide the user deleted since as changed', async () => {
    const { deck, snapshots, write } = setUp();
    const [a] = deck.ids();
    await write('c1', { edit: [a] });
    await snapshots.closeTurn();
    deck.slides = deck.slides.filter(slide => slide.id !== a);
    expect(await snapshots.undo(['c1'])).toEqual({ kind: 'changed', slides: 1 });
  });

  it('does not count a slide a later turn deleted as changed since', async () => {
    const { deck, snapshots, write } = setUp();
    const before = deck.content();
    await write('t1', { add: 1 });
    await snapshots.closeTurn();
    await write('t2', { remove: [deck.ids()[3]] });
    await snapshots.closeTurn();
    expect(await snapshots.undo(['t1'])).toMatchObject({ kind: 'undone', turns: 2 });
    expect(deck.content()).toEqual(before);
  });

  it('keeps the export of a slide from before its first change in the turn, even after a move', async () => {
    const { deck, snapshots, write } = setUp();
    const [a, b] = deck.ids();
    const before = deck.content();
    await write('c1', { move: [[a, 2]] });
    await write('c2', { edit: [a] });
    await write('c3', { edit: [a], move: [[b, 0]] });
    await snapshots.closeTurn();
    await snapshots.undo(['c1']);
    expect(deck.content()).toEqual(before);
  });

  it('puts back the only slide of a deck a turn replaced with a new one', async () => {
    const { deck, snapshots, write } = setUp(1);
    const [a] = deck.ids();
    const before = deck.content();
    await write('c1', { add: 1 });
    await write('c2', { remove: [a] });
    await snapshots.closeTurn();
    await snapshots.undo(['c1']);
    expect(deck.content()).toEqual(before);
  });

  it('has nothing to undo for a turn that is gone', async () => {
    const { snapshots } = setUp();
    expect(await snapshots.undo(['nothing'])).toEqual({ kind: 'gone' });
  });
});

describe('what the pane keeps', () => {
  it('keeps the last ten turns', async () => {
    const { deck, snapshots, write } = setUp();
    for (let turn = 1; turn <= 11; turn += 1) {
      await write(`c${turn}`, { edit: [deck.ids()[0]] });
      await snapshots.closeTurn();
    }
    expect(snapshots.undoable(['c1'])).toBeUndefined();
    expect(snapshots.undoable(['c2'])).toEqual({ slides: 1 });
  });

  it('drops the oldest turns once their exports pass the memory budget, never the turn under way', async () => {
    const { deck, snapshots, write } = setUp(3, { turns: 10, bytes: 0.0001 * MB });
    const [a, b] = deck.ids();
    await write('c1', { edit: [a] });
    await snapshots.closeTurn();
    await write('c2', { edit: [b] });
    expect(snapshots.undoable(['c1'])).toBeUndefined();
    await snapshots.closeTurn();
    expect(snapshots.undoable(['c2'])).toEqual({ slides: 1 });
  });
});
