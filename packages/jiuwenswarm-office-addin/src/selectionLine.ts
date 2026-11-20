/**
 * What the user has selected in the document, as one line above the message box: the cells or chart in Excel, the
 * slides and shapes in PowerPoint and WPS, how many characters in Word. The host's selection event only starts a timer;
 * the selection is read once it has stood still a moment, so a drag reads once. A read that fails (Excel while a
 * cell is being edited) leaves the line as it was.
 */
import { copy } from './copy';
import { HostLine } from './hostLine';

/** What is selected, as each host reads it. */
export type Selection =
  | { kind: 'cells'; ranges: string[] }
  | { kind: 'chart'; name: string }
  | { kind: 'slides'; positions: number[]; shapes: number }
  | { kind: 'text'; characters: number };

const SETTLE_MS = 300;

/** The line, read now and again after each ``listen`` callback. */
export function selectionLine(listen: (moved: () => void) => void, read: () => Promise<Selection>): HostLine {
  const line = new HostLine();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let reads = 0;
  const update = async () => {
    const at = ++reads;
    const selection = await read().catch(() => null);
    // A read that ends after a later one started says nothing new.
    if (selection && at === reads) line.set(named(selection));
  };
  listen(() => {
    clearTimeout(timer);
    timer = setTimeout(() => void update(), SETTLE_MS);
  });
  void update();
  return line;
}

/** ``listen`` for an Office host: its document's selection-changed event. */
export function officeSelectionMoves(document: Office.Document): (moved: () => void) => void {
  return moved => document.addHandlerAsync(Office.EventType.DocumentSelectionChanged, moved);
}

function named(selection: Selection): string | undefined {
  switch (selection.kind) {
    case 'cells':
      return selection.ranges.length ? copy.selectedCells(selection.ranges.join(', ')) : undefined;
    case 'chart':
      return copy.selectedChart(selection.name);
    case 'slides':
      return selection.positions.length ? copy.selectedSlides(runs(selection.positions), selection.shapes) : undefined;
    case 'text':
      return selection.characters ? copy.selectedText(selection.characters) : undefined;
  }
}

/** Positions as runs, as ``2-4, 7``. */
function runs(positions: readonly number[]): string {
  const spans: [number, number][] = [];
  for (const position of [...positions].sort((a, b) => a - b)) {
    const last = spans.at(-1);
    if (last && position === last[1] + 1) last[1] = position;
    else spans.push([position, position]);
  }
  return spans.map(([first, last]) => (first === last ? `${first}` : `${first}-${last}`)).join(', ');
}
