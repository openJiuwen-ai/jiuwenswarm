interface OpenFence {
  character: '`' | '~';
  length: number;
}

function getOpeningFence(line: string): OpenFence | null {
  const match = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line);
  if (!match) return null;
  if (match[1][0] === '`' && match[2].includes('`')) return null;
  return {
    character: match[1][0] as OpenFence['character'],
    length: match[1].length,
  };
}

function isClosingFence(line: string, fence: OpenFence): boolean {
  const pattern = new RegExp(`^ {0,3}\\${fence.character}{${fence.length},}\\s*$`);
  return pattern.test(line);
}

function splitCollapsedTableRows(line: string): string[] {
  const rows: string[] = [];
  let rowStart = 0;

  for (const boundary of line.matchAll(/\|\s*\|/g)) {
    const boundaryStart = boundary.index;
    rows.push(line.slice(rowStart, boundaryStart + 1));
    rowStart = boundaryStart + boundary[0].length - 1;
  }

  rows.push(line.slice(rowStart));
  return rows;
}

function getTableCells(row: string): string[] {
  return row
    .slice(1, -1)
    .split('|')
    .map(cell => cell.trim());
}

function isDelimiterRow(cells: string[]): boolean {
  return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
}

function repairCollapsedTableLine(line: string): string | null {
  const lineMatch = /^( {0,3})(\|.*\|)\s*$/.exec(line);
  if (!lineMatch || !/\|\s*\|/.test(lineMatch[2])) return null;

  const indent = lineMatch[1];
  const rows = splitCollapsedTableRows(lineMatch[2]);
  if (rows.length < 3) return null;

  const cellsByRow = rows.map(getTableCells);
  const columnCount = cellsByRow[0].length;
  const hasConsistentColumns = columnCount > 0 && cellsByRow.every(cells => cells.length === columnCount);
  if (!hasConsistentColumns || !isDelimiterRow(cellsByRow[1])) return null;

  return rows.map(row => `${indent}${row}`).join('\n');
}

/** Restores collapsed GFM table rows without changing fenced code or table-like prose. */
export function repairCollapsedGfmTables(content: string): string {
  if (!/\|\s*\|/.test(content)) return content;

  let openFence: OpenFence | null = null;
  return content.replace(/[^\r\n]*(?:\r\n|\n|\r|$)/g, lineWithEnding => {
    if (!lineWithEnding) return '';

    const endingMatch = /(\r\n|\n|\r)$/.exec(lineWithEnding);
    const ending = endingMatch?.[0] ?? '';
    const line = ending ? lineWithEnding.slice(0, -ending.length) : lineWithEnding;

    if (openFence) {
      if (isClosingFence(line, openFence)) openFence = null;
      return lineWithEnding;
    }

    const nextFence = getOpeningFence(line);
    if (nextFence) {
      openFence = nextFence;
      return lineWithEnding;
    }

    const repaired = repairCollapsedTableLine(line);
    const repairedWithOriginalLineEndings = repaired?.replace(/\n/g, ending || '\n');
    return `${repairedWithOriginalLineEndings ?? line}${ending}`;
  });
}
