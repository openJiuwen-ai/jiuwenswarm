/** A1 addresses of one worksheet, as the Excel tools take and give them: rows and columns counted from 1. */

/** A block of cells; a whole column (``D:D``) or row (``5:5``) is bounded by the sheet's used extent. */
export interface Block {
  top: number;
  left: number;
  bottom: number;
  right: number;
}

const CELL = /^\$?([A-Z]{1,3})\$?(\d{1,7})$/;
const COLUMN = /^\$?([A-Z]{1,3})$/;
const ROW = /^\$?(\d{1,7})$/;

export function columnName(column: number): string {
  let name = '';
  for (let rest = column; rest > 0; rest = Math.floor((rest - 1) / 26)) {
    name = String.fromCharCode(65 + ((rest - 1) % 26)) + name;
  }
  return name;
}

export function columnNumber(name: string): number {
  return [...name].reduce((sum, letter) => sum * 26 + letter.charCodeAt(0) - 64, 0);
}

export function cellName(row: number, column: number): string {
  return `${columnName(column)}${row}`;
}

export function blockName({ top, left, bottom, right }: Block): string {
  const first = cellName(top, left);
  return top === bottom && left === right ? first : `${first}:${cellName(bottom, right)}`;
}

/**
 * The block an A1 address names, a whole column or row reaching to ``extent`` (the last used row and column), or
 * undefined when it is not an address of one sheet's cells.
 */
export function parseBlock(address: string, extent: { rows: number; columns: number }): Block | undefined {
  const [from, to = from, ...rest] = address.trim().toUpperCase().split(':');
  if (rest.length) return undefined;
  const corner = (part: string): [number | undefined, number | undefined] | undefined => {
    const cell = CELL.exec(part);
    if (cell) return [Number(cell[2]), columnNumber(cell[1])];
    const column = COLUMN.exec(part);
    if (column) return [undefined, columnNumber(column[1])];
    const row = ROW.exec(part);
    return row ? [Number(row[1]), undefined] : undefined;
  };
  const start = corner(from);
  const end = corner(to);
  if (!start || !end || (start[0] === undefined) !== (end[0] === undefined)
    || (start[1] === undefined) !== (end[1] === undefined)) return undefined;
  const block = {
    top: start[0] ?? 1, left: start[1] ?? 1,
    bottom: end[0] ?? Math.max(extent.rows, 1), right: end[1] ?? Math.max(extent.columns, 1),
  };
  return block.top <= block.bottom && block.left <= block.right && block.top > 0 && block.left > 0
    ? block : undefined;
}

/** The cell part of an address Excel returns, as ``Sheet1!A1:B2`` or ``'My sheet'!A1``. */
export function cellsOf(address: string): string {
  return address.slice(address.lastIndexOf('!') + 1);
}

export function overlap(a: Block, b: Block): Block | undefined {
  const block = { top: Math.max(a.top, b.top), left: Math.max(a.left, b.left),
    bottom: Math.min(a.bottom, b.bottom), right: Math.min(a.right, b.right) };
  return block.top <= block.bottom && block.left <= block.right ? block : undefined;
}

export function contains(outer: Block, inner: Block): boolean {
  return inner.top >= outer.top && inner.bottom <= outer.bottom && inner.left >= outer.left
    && inner.right <= outer.right;
}
