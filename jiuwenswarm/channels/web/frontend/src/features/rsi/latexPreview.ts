export interface LatexPreviewOptions {
  resolveImage?: (path: string) => string;
}

function stripComments(latex: string): string {
  return latex.replace(/(?<!\\)%[^\n\r]*/g, '');
}

function findMatchingBrace(source: string, openIndex: number): number {
  let depth = 0;
  for (let i = openIndex; i < source.length; i++) {
    const ch = source[i];
    if (ch === '\\' && i + 1 < source.length) {
      i += 1;
      continue;
    }
    if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return -1;
}

function unwrapSizingCommands(source: string): string {
  const pattern = /\\(resizebox|scalebox|adjustbox)\b/;
  let result = source;
  let match = pattern.exec(result);
  while (match) {
    const start = match.index;
    let j = start + match[0].length;
    while (j < result.length && result[j] === '[') {
      const close = result.indexOf(']', j);
      if (close < 0) break;
      j = close + 1;
    }
    const argStarts: number[] = [];
    while (j < result.length && result[j] === '{') {
      const close = findMatchingBrace(result, j);
      if (close < 0) break;
      argStarts.push(j);
      j = close + 1;
    }
    const requiredArgCount = match[1] === 'resizebox' ? 3 : 2;
    if (argStarts.length >= requiredArgCount) {
      const contentStart = argStarts[requiredArgCount - 1];
      const contentEnd = findMatchingBrace(result, contentStart);
      result = result.slice(0, start) + result.slice(contentStart + 1, contentEnd) + result.slice(contentEnd + 1);
    } else {
      result = result.slice(0, start) + result.slice(j);
    }
    match = pattern.exec(result);
  }
  return result;
}

function readCommand(source: string, command: string): string {
  const marker = `\\${command}{`;
  const start = source.indexOf(marker);
  if (start < 0) return '';

  let index = start + marker.length;
  let depth = 1;
  let value = '';
  while (index < source.length && depth > 0) {
    const char = source[index];
    if (char === '{') depth += 1;
    else if (char === '}') depth -= 1;
    if (depth > 0) value += char;
    index += 1;
  }
  return value;
}

function lastCommandValue(source: string, command: string): string {
  const matches = source.match(new RegExp(`\\\\${command}(?:\\[[^\\]]*\\])?\\{([^{}]*)\\}`, 'g')) ?? [];
  if (matches.length === 0) return '';
  const match = matches[matches.length - 1].match(/\{([^{}]*)\}$/);
  return match?.[1] ?? '';
}

function convertInline(value: string): string {
  const mathSegments: string[] = [];
  const protectedValue = value.replace(/\$\$[\s\S]*?\$\$|\$[^$]*\$/g, (match) => {
    mathSegments.push(match);
    return `%%%MATH${mathSegments.length - 1}%%%`;
  });

  const converted = protectedValue
    .replace(/\\textbf\{([^{}]*)\}/g, '**$1**')
    .replace(/\\textit\{([^{}]*)\}/g, '*$1*')
    .replace(/\\emph\{([^{}]*)\}/g, '*$1*')
    .replace(/\\texttt\{([^{}]*)\}/g, '`$1`')
    .replace(/\\underline\{([^{}]*)\}/g, '<u>$1</u>')
    .replace(/\\href\{([^{}]*)\}\{([^{}]*)\}/g, '[$2]($1)')
    .replace(/\\url\{([^{}]*)\}/g, '[$1]($1)')
    .replace(/\\footnote\{([^{}]*)\}/g, ' ($1)')
    .replace(/\\cite[^\s{]*(?:\[[^\]]*\])?\{([^{}]*)\}/g, '[$1]')
    .replace(/\\(?:ref|eqref|autoref|pageref)\{([^{}]*)\}/g, '[$1]')
    .replace(/\\label\{([^{}]*)\}/g, '')
    .replace(/\\textcolor\{[^{}]*\}\{([^{}]*)\}/g, '$1')
    .replace(/\\cmark/g, '✓')
    .replace(/\\xmark/g, '✗')
    .replace(/\\%/g, '%')
    .replace(/\\&/g, '&')
    .replace(/\\_/g, '_')
    .replace(/\\#/g, '#')
    .replace(/\\\$/g, '$')
    .replace(/\\\\/g, '<br>')
    .replace(/~/g, ' ')
    .replace(/\\(?:centering|raggedright|raggedleft|small|footnotesize|scriptsize|large|Large|LARGE|huge|Huge|normalfont|bfseries|mdseries|itshape|upshape|rmfamily|sffamily|ttfamily)/g, '')
    .replace(/\\(?:[A-Za-z]+)\*?(?:\[[^\]]*\])?(?:\{[^{}]*\})?/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .trim();

  return converted.replace(/%%%MATH(\d+)%%%/g, (_match, index: string) => mathSegments[Number(index)] ?? '');
}

const BEGIN_TABULAR = '\\begin{tabular}';
const END_TABULAR = '\\end{tabular}';

interface TabularCell {
  text: string;
  columnSpan: number;
  rowSpan: number;
}

function skipTabularHeader(source: string, fromIdx: number): number {
  let j = fromIdx;
  while (j < source.length && source[j] === '[') {
    const close = source.indexOf(']', j);
    if (close < 0) break;
    j = close + 1;
  }
  if (j < source.length && source[j] === '{') {
    const close = findMatchingBrace(source, j);
    if (close < 0) return -1;
    j = close + 1;
  }
  return j;
}

function findTabularEnd(source: string, beginIdx: number): number {
  let depth = 0;
  let index = beginIdx;
  while (index < source.length) {
    if (source.startsWith(BEGIN_TABULAR, index)) {
      depth += 1;
      index += BEGIN_TABULAR.length;
      continue;
    }
    if (source.startsWith(END_TABULAR, index)) {
      depth -= 1;
      if (depth === 0) return index;
      index += END_TABULAR.length;
      continue;
    }
    index += 1;
  }
  return -1;
}

function countTabularColumns(spec: string): number {
  const normalized = spec.replace(/[@<>!]\{[^{}]*\}/g, '').replace(/\|/g, '');
  return (normalized.match(/(?:[lcrX]|p\{[^{}]*\}|m\{[^{}]*\}|b\{[^{}]*\})/g) ?? []).length;
}

function extractTabularBody(source: string): { body: string; columnCount: number } {
  const beginIdx = source.indexOf(BEGIN_TABULAR);
  if (beginIdx < 0) return { body: source, columnCount: 0 };

  let index = beginIdx + BEGIN_TABULAR.length;
  while (index < source.length && source[index] === '[') {
    const close = source.indexOf(']', index);
    if (close < 0) return { body: source, columnCount: 0 };
    index = close + 1;
  }
  if (index >= source.length || source[index] !== '{') return { body: source, columnCount: 0 };

  const specEnd = findMatchingBrace(source, index);
  if (specEnd < 0) return { body: source, columnCount: 0 };
  const spec = source.slice(index + 1, specEnd);
  const bodyStart = specEnd + 1;
  const endIdx = findTabularEnd(source, beginIdx);
  if (endIdx < 0 || endIdx < bodyStart) return { body: source, columnCount: 0 };

  return {
    body: source.slice(bodyStart, endIdx),
    columnCount: countTabularColumns(spec),
  };
}

function flattenNestedTabulars(source: string): string {
  let result = source;
  let changed = true;
  while (changed) {
    changed = false;
    while (result.length > 0) {
      const beginIdx = result.indexOf(BEGIN_TABULAR);
      if (beginIdx < 0) break;
      const contentStart = skipTabularHeader(result, beginIdx + BEGIN_TABULAR.length);
      if (contentStart < 0) break;
      const endIdx = findTabularEnd(result, beginIdx);
      if (endIdx < 0 || endIdx < contentStart) break;
      const inner = result.slice(contentStart, endIdx);
      if (inner.includes(BEGIN_TABULAR)) {
        result = result.slice(0, beginIdx) + inner + result.slice(endIdx + END_TABULAR.length);
        continue;
      }
      const flattened = inner.replace(/\\\\(?:\s*\[[^\]]*\])?/g, ' ').trim();
      result = result.slice(0, beginIdx) + flattened + result.slice(endIdx + END_TABULAR.length);
      changed = true;
    }
  }
  return result;
}

function splitTabularRows(source: string): string[] {
  const rows: string[] = [];
  let rowStart = 0;
  let braceDepth = 0;
  let index = 0;

  while (index < source.length) {
    const char = source[index];
    if (char === '\\') {
      if (source.startsWith('\\\\', index)) {
        if (braceDepth === 0) {
          let rowEnd = index;
          let nextIndex = index + 2;
          if (source[nextIndex] === '*') nextIndex += 1;
          while (nextIndex < source.length && /\s/.test(source[nextIndex])) nextIndex += 1;
          if (source[nextIndex] === '[') {
            const close = source.indexOf(']', nextIndex);
            if (close >= 0) nextIndex = close + 1;
          }
          rows.push(source.slice(rowStart, rowEnd));
          rowStart = nextIndex;
          index = nextIndex;
          continue;
        }
        index += 2;
        continue;
      }
      index += 2;
      continue;
    }
    if (char === '{') braceDepth += 1;
    else if (char === '}') braceDepth = Math.max(0, braceDepth - 1);
    index += 1;
  }

  rows.push(source.slice(rowStart));
  return rows.map((row) => row.trim()).filter(Boolean);
}

function splitTabularCells(source: string): string[] {
  const cells: string[] = [];
  let cellStart = 0;
  let braceDepth = 0;
  let index = 0;

  while (index < source.length) {
    const char = source[index];
    if (char === '\\') {
      index += 2;
      continue;
    }
    if (char === '{') braceDepth += 1;
    else if (char === '}') braceDepth = Math.max(0, braceDepth - 1);
    else if (char === '&' && braceDepth === 0) {
      cells.push(source.slice(cellStart, index));
      cellStart = index + 1;
    }
    index += 1;
  }

  cells.push(source.slice(cellStart));
  return cells.map((cell) => cell.trim());
}

function normalizeTabularCell(source: string): string {
  return convertInline(source)
    .replace(/<br\s*\/?>/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\|/g, '\\|');
}

function readCommandArgument(source: string, argumentStart: number): { content: string; end: number } {
  const end = findMatchingBrace(source, argumentStart);
  if (end < 0) return { content: source.slice(argumentStart + 1), end: source.length };
  return { content: source.slice(argumentStart + 1, end), end };
}

function parseTabularCell(source: string): TabularCell {
  const trimmed = source.trim();
  const multicolumnMatch = /^\\multicolumn\{(\d+)\}\s*\{[^{}]*\}\s*\{/.exec(trimmed);
  if (multicolumnMatch) {
    const contentStart = trimmed.indexOf('{', multicolumnMatch[0].length - 1);
    const { content } = readCommandArgument(trimmed, contentStart);
    const inner = parseTabularCell(content);
    return {
      text: inner.text,
      columnSpan: Math.max(1, Number.parseInt(multicolumnMatch[1], 10) || 1),
      rowSpan: inner.rowSpan,
    };
  }

  const multirowMatch = /^\\multirow\{(\d+)\}(?:\[[^\]]*\])?\s*\{[^{}]*\}\s*\{/.exec(trimmed);
  if (multirowMatch) {
    const contentStart = trimmed.indexOf('{', multirowMatch[0].length - 1);
    const { content } = readCommandArgument(trimmed, contentStart);
    return {
      text: normalizeTabularCell(content),
      columnSpan: 1,
      rowSpan: Math.max(1, Number.parseInt(multirowMatch[1], 10) || 1),
    };
  }

  return {
    text: normalizeTabularCell(trimmed),
    columnSpan: 1,
    rowSpan: 1,
  };
}

function convertTabular(content: string): string {
  const { body: tabularBody, columnCount: templateColumnCount } = extractTabularBody(content);
  const body = flattenNestedTabulars(tabularBody)
    .replace(/\\(?:toprule|midrule|bottomrule|hline|cline)(?:\([^)]*\))?(?:\[[^\]]*\])?\{[^{}]*\}/g, ' ')
    .replace(/\\(?:toprule|midrule|bottomrule|hline|cline)/g, ' ')
    .replace(/\\cmidrule(?:\([^)]*\))?(?:\[[^\]]*\])?\{[^{}]*\}/g, ' ')
    .replace(/\\vspace\*?(?:\[[^\]]*\])?\{[^{}]*\}/g, ' ')
    .replace(/\\caption\*?\{[^{}]*\}/g, ' ')
    .replace(/\\label\{[^{}]*\}/g, ' ')
    .replace(/\\centering/g, ' ')
    .replace(/\\tabularnewline/g, '\\\\')
    .replace(/^\s*\[[^\]]*\]/, '')
    .trim();

  const rowSources = splitTabularRows(body).filter(Boolean);
  if (rowSources.length === 0) return '';

  const parsedRows = rowSources.map((row) => splitTabularCells(row).map((cell) => parseTabularCell(cell)));
  const columnCount = Math.max(
    templateColumnCount,
    ...parsedRows.map((cells) => cells.reduce((total, cell) => total + cell.columnSpan, 0)),
    1,
  );

  const tableRows = parsedRows.map(() => Array.from({ length: columnCount }, () => ''));
  const activeRowSpans: Array<{ remaining: number; text: string } | null> = Array.from({ length: columnCount }, () => null);
  parsedRows.forEach((cells, rowIndex) => {
    const prefilledColumns = new Set<number>();
    for (let columnIndex = 0; columnIndex < columnCount; columnIndex += 1) {
      const activeSpan = activeRowSpans[columnIndex];
      if (!activeSpan || activeSpan.remaining <= 0) continue;
      tableRows[rowIndex][columnIndex] = activeSpan.text;
      prefilledColumns.add(columnIndex);
    }

    let columnIndex = 0;
    for (const cell of cells) {
      if (columnIndex >= columnCount) break;
      if (cell.text || !prefilledColumns.has(columnIndex)) {
        tableRows[rowIndex][columnIndex] = cell.text;
      }
      const span = Math.max(1, Math.min(cell.columnSpan, columnCount - columnIndex));
      if (cell.rowSpan > 1) {
        activeRowSpans[columnIndex] = { remaining: cell.rowSpan, text: cell.text };
      }
      columnIndex += span;
    }

    for (let activeColumnIndex = 0; activeColumnIndex < columnCount; activeColumnIndex += 1) {
      const activeSpan = activeRowSpans[activeColumnIndex];
      if (!activeSpan) continue;
      activeSpan.remaining -= 1;
      if (activeSpan.remaining <= 0) activeRowSpans[activeColumnIndex] = null;
    }
  });

  const markdownRows = tableRows.map((cells) => `| ${cells.join(' | ')} |`);
  const separator = `| ${Array.from({ length: columnCount }, () => '---').join(' | ')} |`;
  return [markdownRows[0], separator, ...markdownRows.slice(1)].join('\n');
}

function convertFigure(content: string, options: LatexPreviewOptions): string {
  const caption = convertInline(readCommand(content, 'caption'));
  const imagePath = lastCommandValue(content, 'includegraphics');
  const resolvedImage = imagePath ? options.resolveImage?.(imagePath) : undefined;
  const isImage = /\.(?:png|jpe?g|gif|webp|svg)$/i.test(imagePath);
  if (resolvedImage && isImage) {
    return `![${caption}](${resolvedImage})`;
  }
  return caption ? `**Figure: ${caption}**` : '**Figure**';
}

function convertTableEnvironment(table: string): string {
  const caption = convertInline(readCommand(table, 'caption'));
  const unwrapped = unwrapSizingCommands(table);
  const tabular = convertTabular(unwrapped);
  return [tabular, caption ? `*${caption}*` : ''].filter(Boolean).join('\n\n');
}

export function latexToMarkdown(latex: string, options: LatexPreviewOptions = {}): string {
  const withoutComments = stripComments(latex);
  const unwrapped = unwrapSizingCommands(withoutComments);
  const documentMatch = unwrapped.match(/\\begin\{document\}([\s\S]*?)\\end\{document\}/i);
  const bodySource = documentMatch?.[1] ?? unwrapped;
  const title = readCommand(unwrapped, 'title');
  const author = readCommand(unwrapped, 'author');

  let body = bodySource
    .replace(/\\maketitle|\\newpage|\\clearpage|\\bibliography\{[^{}]*\}|\\printbibliography|\\balance/g, '')
    .replace(/\\begin\{abstract\}([\s\S]*?)\\end\{abstract\}/gi, (_match, abstract: string) => `**Abstract**\n\n${abstract.trim()}`)
    .replace(/\\section\*?\{([^{}]*)\}/g, (_match, title: string) => `## ${convertInline(title)}`)
    .replace(/\\subsection\*?\{([^{}]*)\}/g, (_match, title: string) => `### ${convertInline(title)}`)
    .replace(/\\subsubsection\*?\{([^{}]*)\}/g, (_match, title: string) => `#### ${convertInline(title)}`)
    .replace(/\\begin\{(?:figure|figure\*)\}([\s\S]*?)\\end\{(?:figure|figure\*)\}/g, (_match, figure: string) => convertFigure(figure, options))
    .replace(/\\begin\{wraptable\}(?:\[[^\]]*\])?\{[^{}]*\}\{[^{}]*\}([\s\S]*?)\\end\{wraptable\}/g, (_match, table: string) => convertTableEnvironment(table))
    .replace(/\\begin\{longtable\}(?:\[[^\]]*\])?\{[^{}]*\}([\s\S]*?)\\end\{longtable\}/g, (_match, table: string) => convertTabular(table))
    .replace(/\\begin\{(?:table|table\*)\}(?:\[[^\]]*\])?([\s\S]*?)\\end\{(?:table|table\*)\}/g, (_match, table: string) => convertTableEnvironment(table))
    .replace(/\\begin\{(?:equation|align|aligned|gather|multline|eqnarray)\*?\}([\s\S]*?)\\end\{(?:equation|align|aligned|gather|multline|eqnarray)\*?\}/g, (_match, equation: string) => `$$${equation.trim()}$$`)
    .replace(/\\\[([\s\S]*?)\\\]/g, (_match, equation: string) => `$$${equation.trim()}$$`)
    .replace(/\\\(([\s\S]*?)\\\)/g, (_match, equation: string) => `$${equation.trim()}$`)
    .replace(/\\begin\{(?:itemize|enumerate|description)\*?\}([\s\S]*?)\\end\{(?:itemize|enumerate|description)\*?\}/g, (_match, list: string) => {
      return list
        .split(/\\item(?![A-Za-z])/)
        .map((item) => item.trim())
        .filter(Boolean)
        .map((item) => {
          const description = item.match(/^\[([^\]]*)\]\s*/);
          const content = description ? item.slice(description[0].length) : item;
          return `- ${description ? `**${convertInline(description[1])}** ` : ''}${convertInline(content)}`;
        })
        .join('\n');
    });

  const header = [
    title ? `# ${convertInline(title)}` : '',
    author ? convertInline(author) : '',
  ].filter(Boolean).join('\n\n');

  const paragraphs = body
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean)
    .map((paragraph) => {
      const isTableBlock = paragraph.startsWith('|');
      return isTableBlock ? paragraph : convertInline(paragraph);
    })
    .filter(Boolean);

  return [header, ...paragraphs].filter(Boolean).join('\n\n');
}
