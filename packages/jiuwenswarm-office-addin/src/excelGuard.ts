/**
 * What the Excel tools refuse to write, whatever the user's approval settings: formulas that reach the network, a
 * program or a file outside the workbook, and links to another workbook, which make Excel contact another computer.
 * The industry add-in asks the user in its pane for these; the approval here is the backend's, before the call, so
 * the pane refuses them outright.
 */
import type { HostWrite } from './officeCodeRunner';

/** Functions that fetch, run or read outside the workbook, or build a reference from text (as the industry list). */
const BLOCKED_FUNCTIONS = new Set(['WEBSERVICE', 'IMPORTDATA', 'INDIRECT', 'IMPORTXML', 'IMPORTHTML', 'IMPORTFEED',
  'FILTERXML', 'RTD', 'REGISTER.ID', 'REGISTER', 'CALL', 'EVALUATE', 'FORMULA', 'IMAGE', 'FILES', 'DIRECTORY', 'DDE',
  'FOPEN', 'FWRITE', 'FCLOSE', 'INFO', 'STOCKHISTORY', 'STOCKSERIES', 'TRANSLATE', 'CUBEKPIMEMBER', 'CUBEMEMBER',
  'CUBEMEMBERPROPERTY', 'CUBERANKEDMEMBER', 'CUBESET', 'CUBESETCOUNT', 'CUBEVALUE', 'EXEC', 'HYPERLINK']);
/** How Excel tells a formula from text in a cell: the leading ``=``, or a sign or ``@`` before a name or bracket. */
const FORMULA = /^\s*(?:=|\{=|[+\-@]\s*[A-Za-z_[('"])/;
const STRINGS = /"(?:[^"]|"")*"/g;
const QUOTED_NAMES = /'(?:[^']|'')*'/g;
/** A name: a function called or passed (``MAP(A1:A3, WEBSERVICE)``), not a sheet (``Info!A1``). */
const NAME = /(?:^|[^A-Za-z0-9_.])(?:_xl\w*\.)*([A-Za-z][A-Za-z0-9_.]*)(?![A-Za-z0-9_.]|\s*!)/g;
const BRACKETS = /\[[^[\]]*\]/g;
const SEPARATORS = String.raw`\s!'"()[\]{},;+\-*/^&=<>%#@`;
/** ``[Book.xlsx]Sheet1!A1``, ``data.csv!A1``, or a path or address outside the quotes. */
const LINKS = [
  new RegExp(String.raw`\[[^[\]]*\][^${SEPARATORS}]*!`),
  new RegExp(String.raw`[^${SEPARATORS}:]+\.(?:xl[a-z]{0,2}|xml|html?|csv|txt|prn|ods|dbf)(?![A-Za-z0-9])`
    + String.raw`[^${SEPARATORS}]*!`, 'i'),
  /\\\\|\/\/|[A-Za-z]{2,}:[\\/]/,
];

/** Why a formula is refused, or undefined for text that is no formula and a formula that stays in the workbook. */
export function formulaThreat(text: string): string | undefined {
  if (!FORMULA.test(text)) return undefined;
  const code = text.replace(STRINGS, '""');
  for (const match of code.replace(QUOTED_NAMES, 'Sheet').replace(BRACKETS, '[]').matchAll(NAME)) {
    const name = match[1].toUpperCase();
    if (BLOCKED_FUNCTIONS.has(name)) {
      return `The formula ${clip(text)} uses ${name}, which can reach the network, a program or files outside this `
        + 'workbook.';
    }
  }
  const linking = 'links to another workbook or file, which can make Excel contact another computer. Write it '
    + 'without that link, or ask the user to add the link in Excel themselves.';
  const quoted = code.match(QUOTED_NAMES) ?? [];
  if (quoted.some(name => /[[\\/]|:[\\/]/.test(name))) return `The formula ${clip(text)} ${linking}`;
  const bare = code.replace(QUOTED_NAMES, 'Sheet');
  if (bare.includes('|')) return `The formula ${clip(text)} holds a DDE link, which runs another program.`;
  return LINKS.some(link => link.test(bare)) ? `The formula ${clip(text)} ${linking}` : undefined;
}

function clip(text: string): string {
  return JSON.stringify(text.length > 120 ? `${text.slice(0, 120)}…` : text);
}

/** The first refused formula among the strings in ``value``, however deeply nested. */
export function firstThreat(value: unknown): string | undefined {
  if (typeof value === 'string') return formulaThreat(value);
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = firstThreat(item);
      if (found) return found;
    }
  } else if (value && typeof value === 'object') {
    return firstThreat(Object.values(value));
  }
  return undefined;
}

/**
 * Each property ``value`` writes under ``name``, nested ones too: an assignment writes one, ``set({...})`` and
 * ``setCellProperties`` write those they are given.
 */
function* properties(name: string, value: unknown): Generator<[string, unknown]> {
  yield [name, value];
  if (value && typeof value === 'object') {
    for (const [key, item] of Object.entries(value)) yield* properties(key, item);
  }
}

/** The runner's screen for Excel: formulas as above, links out of the workbook, and refreshes from outside it. */
export function screenExcelWrite({ path, name, values }: HostWrite): string | undefined {
  for (const [key, value] of values.flatMap(item => [...properties(name, item)])) {
    if (key === 'hyperlink' && value && typeof value === 'object' && 'address' in value) {
      return 'A hyperlink to an address outside this workbook can open another computer\'s files or a web page.';
    }
    if (/^formulas(?:R1C1)?Local$/.test(key)) {
      return 'Formulas in the user\'s language cannot be checked here; write them in English with formulas or '
        + 'formulasR1C1.';
    }
  }
  if (name === 'insertWorksheetsFromBase64') {
    return 'Inserting sheets from another workbook can bring its links to other computers along.';
  }
  if (/^requestRefresh/.test(name)
    || (/^refresh/.test(name) && path.some(step => step === 'dataConnections' || step === 'linkedWorkbooks'))) {
    return 'Refreshing data connections, linked workbooks or linked data types reads from outside this workbook.';
  }
  return firstThreat(values);
}
