export type SymphonyCommandAction =
  | { kind: 'search'; query: string }
  | {
      kind: 'read';
      target: string;
      targetKind: 'skill' | 'metadata' | 'file';
      startLine?: number;
      endLine?: number;
      lastLines?: number;
    }
  | { kind: 'browse'; path?: string }
  | { kind: 'find'; query?: string }
  | { kind: 'location' }
  | { kind: 'command'; commandName?: string };

export interface SymphonyCommandLabel {
  key: string;
  values?: Record<string, string | number>;
}

const VALUE_OPTIONS = new Set([
  '-A',
  '-B',
  '-C',
  '-g',
  '-m',
  '-t',
  '--after-context',
  '--before-context',
  '--context',
  '--glob',
  '--max-count',
  '--type',
  '--type-add',
  '--type-not',
]);

const SYMPHONY_COMMAND_TOOLS = new Set([
  'skill_index',
  'skill_directory',
  'skillindex',
  'skill_inventory',
  'skill_inventory_command',
]);

function tokenizeShell(command: string): string[] {
  const tokens: string[] = [];
  let current = '';
  let quote: '"' | "'" | null = null;
  let escaped = false;

  const flush = () => {
    if (current) {
      tokens.push(current);
      current = '';
    }
  };

  for (let index = 0; index < command.length; index += 1) {
    const char = command[index];
    if (escaped) {
      current += char;
      escaped = false;
      continue;
    }
    if (char === '\\' && quote !== "'") {
      escaped = true;
      continue;
    }
    if (quote) {
      if (char === quote) {
        quote = null;
      } else {
        current += char;
      }
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      continue;
    }
    if (/\s/.test(char)) {
      flush();
      continue;
    }
    if (char === '|' || char === ';') {
      flush();
      tokens.push(char);
      continue;
    }
    if (char === '&' && command[index + 1] === '&') {
      flush();
      tokens.push('&&');
      index += 1;
      continue;
    }
    current += char;
  }
  if (escaped) {
    current += '\\';
  }
  flush();
  return tokens;
}

function splitPipeline(tokens: string[]): string[][] {
  const segments: string[][] = [[]];
  for (const token of tokens) {
    if (token === '|' || token === ';' || token === '&&') {
      if (segments[segments.length - 1].length > 0) {
        segments.push([]);
      }
      continue;
    }
    segments[segments.length - 1].push(token);
  }
  return segments.filter(segment => segment.length > 0);
}

function executableName(token = ''): string {
  return token.split(/[\\/]/).pop()?.toLowerCase() || '';
}

function shorten(value: string, maxLength = 72): string {
  const normalized = value.trim().replace(/\s+/g, ' ');
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength - 1)}…`;
}

function findSearchQuery(tokens: string[]): string | undefined {
  for (let index = 1; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token === '-e' || token === '--regexp') {
      return tokens[index + 1];
    }
    if (token.startsWith('--regexp=')) {
      return token.slice('--regexp='.length);
    }
    if (VALUE_OPTIONS.has(token)) {
      index += 1;
      continue;
    }
    if (token === '--') {
      return tokens[index + 1];
    }
    if (!token.startsWith('-')) {
      return token;
    }
  }
  return undefined;
}

function findFindQuery(tokens: string[]): string | undefined {
  const patternOptions = new Set(['-name', '-iname', '-path', '-ipath', '-regex', '-iregex']);
  for (let index = 1; index < tokens.length - 1; index += 1) {
    if (patternOptions.has(tokens[index])) {
      return tokens[index + 1].replace(/^\*+|\*+$/g, '');
    }
  }
  return undefined;
}

function pathTarget(path: string | undefined): {
  target: string;
  targetKind: 'skill' | 'metadata' | 'file';
} {
  const normalized = String(path || '').replace(/^["']|["']$/g, '');
  const parts = normalized.split(/[\\/]/).filter(Boolean);
  const fileName = parts[parts.length - 1] || normalized || 'Skill';
  const parent = parts[parts.length - 2];
  if (/^SKILL\.md$/i.test(fileName) && parent) {
    return { target: parent, targetKind: 'skill' };
  }
  if (/^META\.md$/i.test(fileName) && parent) {
    return { target: parent, targetKind: 'metadata' };
  }
  return { target: shorten(normalized || fileName), targetKind: 'file' };
}

function firstPath(tokens: string[], startIndex = 1): string | undefined {
  for (let index = startIndex; index < tokens.length; index += 1) {
    if (tokens[index] === '--') {
      return tokens[index + 1];
    }
    if (!tokens[index].startsWith('-')) {
      return tokens[index];
    }
  }
  return undefined;
}

function browsePath(tokens: string[], commandName: string): string | undefined {
  const treeValueOptions = new Set(['-L', '-P', '-I', '--charset', '--filelimit', '--sort']);
  for (let index = 1; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (token === '--') {
      return tokens[index + 1];
    }
    if (commandName === 'tree' && treeValueOptions.has(token)) {
      index += 1;
      continue;
    }
    if (!token.startsWith('-')) {
      return token;
    }
  }
  return undefined;
}

function lineLimit(tokens: string[]): number | undefined {
  for (let index = 1; index < tokens.length; index += 1) {
    const compact = tokens[index].match(/^-(\d+)$/);
    if (compact) {
      return Number(compact[1]);
    }
    if (tokens[index] === '-n' || tokens[index] === '--lines') {
      const value = Number(tokens[index + 1]);
      return Number.isFinite(value) && value > 0 ? value : undefined;
    }
    const assigned = tokens[index].match(/^--lines=(\d+)$/);
    if (assigned) {
      return Number(assigned[1]);
    }
  }
  return undefined;
}

function headTailPath(tokens: string[]): string | undefined {
  for (let index = 1; index < tokens.length; index += 1) {
    const token = tokens[index];
    if (/^-\d+$/.test(token) || /^--lines=\d+$/.test(token)) {
      continue;
    }
    if (token === '-n' || token === '--lines') {
      index += 1;
      continue;
    }
    if (!token.startsWith('-')) {
      return token;
    }
  }
  return undefined;
}

function sedRange(tokens: string[]): { startLine: number; endLine?: number } | undefined {
  for (const token of tokens.slice(1)) {
    const match = token.match(/^(\d+)(?:,(\d+|\$))?p$/);
    if (!match) {
      continue;
    }
    return {
      startLine: Number(match[1]),
      endLine: match[2] && match[2] !== '$' ? Number(match[2]) : undefined,
    };
  }
  return undefined;
}

function sedPath(tokens: string[]): string | undefined {
  const rangeIndex = tokens.findIndex(token => /^(\d+)(?:,(\d+|\$))?p$/.test(token));
  if (rangeIndex < 0) {
    return undefined;
  }
  return tokens.slice(rangeIndex + 1).find(token => !token.startsWith('-'));
}

function readAction(path: string | undefined, range?: { startLine?: number; endLine?: number; lastLines?: number }): SymphonyCommandAction {
  return {
    kind: 'read',
    ...pathTarget(path),
    ...range,
  };
}

export function isSymphonyCommandTool(name: string): boolean {
  const normalized = name
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, '_');
  return SYMPHONY_COMMAND_TOOLS.has(normalized.split(/[./:]/).pop() || '');
}

export function parseSymphonyCommandAction(args: Record<string, unknown> | null | undefined): SymphonyCommandAction | null {
  const operation = typeof args?.operation === 'string' ? args.operation.trim().toLowerCase() : '';
  const structuredPaths = Array.isArray(args?.paths)
    ? args.paths.filter((value): value is string => typeof value === 'string' && value.trim().length > 0)
    : [];
  if (operation === 'search') {
    const query = typeof args?.query === 'string' ? shorten(args.query) : '';
    return query ? { kind: 'search', query } : { kind: 'command', commandName: operation };
  }
  if (operation === 'list') {
    const path = structuredPaths[0]?.trim();
    return {
      kind: 'browse',
      path: path && path !== '/' ? shorten(path) : undefined,
    };
  }
  if (operation === 'read') {
    const path = structuredPaths[0]?.trim();
    if (!path) {
      return { kind: 'command', commandName: operation };
    }
    const readMode = typeof args?.read_mode === 'string' ? args.read_mode.trim().toLowerCase() : 'full';
    if (readMode === 'head') {
      const lineCount = typeof args?.line_count === 'number' && args.line_count > 0 ? args.line_count : 10;
      return readAction(path, { startLine: 1, endLine: lineCount });
    }
    if (readMode === 'range') {
      const startLine = typeof args?.start_line === 'number' ? args.start_line : undefined;
      const endLine = typeof args?.end_line === 'number' ? args.end_line : undefined;
      return readAction(path, { startLine, endLine });
    }
    return readAction(path);
  }

  // Historical structured operations retained only for replay display.
  const pattern = typeof args?.pattern === 'string' ? shorten(args.pattern) : undefined;
  const path = typeof args?.path === 'string' ? args.path.trim() : undefined;
  if (operation === 'grep') {
    return pattern ? { kind: 'search', query: pattern } : { kind: 'command', commandName: operation };
  }
  if (operation === 'find') {
    return { kind: 'find', query: pattern };
  }
  if (operation === 'ls' || operation === 'tree') {
    return {
      kind: 'browse',
      path: path && path !== 'skills:/' ? shorten(path) : undefined,
    };
  }
  if (operation === 'stat') {
    const skillId = typeof args?.skill_id === 'string' ? args.skill_id.trim() : '';
    return skillId
      ? { kind: 'read', target: skillId, targetKind: 'metadata' }
      : { kind: 'command', commandName: operation };
  }

  const rawCommand = typeof args?.command === 'string' ? args.command.trim() : '';
  if (!rawCommand) {
    return null;
  }
  const segments = splitPipeline(tokenizeShell(rawCommand));
  const primary = segments[0] || [];
  const commandName = executableName(primary[0]);

  if (commandName === 'rg' || commandName === 'grep' || commandName === 'egrep') {
    const query = findSearchQuery(primary);
    if (query) {
      return { kind: 'search', query: shorten(query) };
    }
  }

  if (commandName === 'find') {
    const query = findFindQuery(primary);
    return { kind: 'find', query: query ? shorten(query) : undefined };
  }

  if (commandName === 'ls' || commandName === 'tree') {
    const path = browsePath(primary, commandName);
    return {
      kind: 'browse',
      path: path && path !== '.' && path !== '/' ? shorten(path) : undefined,
    };
  }

  if (commandName === 'pwd') {
    return { kind: 'location' };
  }

  if (commandName === 'sed') {
    const range = sedRange(primary);
    const path = sedPath(primary);
    if (range && path) {
      return readAction(path, range);
    }
  }

  if (commandName === 'head' || commandName === 'tail') {
    const limit = lineLimit(primary) ?? 10;
    const path = headTailPath(primary);
    if (path) {
      return commandName === 'tail' ? readAction(path, { lastLines: limit }) : readAction(path, { startLine: 1, endLine: limit });
    }
  }

  if (commandName === 'cat') {
    const path = firstPath(primary);
    const pipedHead = segments.find(segment => executableName(segment[0]) === 'head');
    const pipedTail = segments.find(segment => executableName(segment[0]) === 'tail');
    const pipedSed = segments.find(segment => executableName(segment[0]) === 'sed');
    if (pipedHead) {
      return readAction(path, { startLine: 1, endLine: lineLimit(pipedHead) ?? 10 });
    }
    if (pipedTail) {
      return readAction(path, { lastLines: lineLimit(pipedTail) ?? 10 });
    }
    const range = pipedSed ? sedRange(pipedSed) : undefined;
    return readAction(path, range);
  }

  return { kind: 'command', commandName: commandName || undefined };
}

export function getSymphonyCommandLabel(action: SymphonyCommandAction): SymphonyCommandLabel {
  if (action.kind === 'search') {
    return { key: 'chatUi.toolGroup.symphony.search', values: { query: action.query } };
  }
  if (action.kind === 'find') {
    return action.query ? { key: 'chatUi.toolGroup.symphony.find', values: { query: action.query } } : { key: 'chatUi.toolGroup.symphony.findGeneric' };
  }
  if (action.kind === 'browse') {
    return action.path ? { key: 'chatUi.toolGroup.symphony.browsePath', values: { path: action.path } } : { key: 'chatUi.toolGroup.symphony.browse' };
  }
  if (action.kind === 'location') {
    return { key: 'chatUi.toolGroup.symphony.location' };
  }
  if (action.kind === 'command') {
    return action.commandName
      ? { key: 'chatUi.toolGroup.symphony.commandNamed', values: { command: action.commandName } }
      : { key: 'chatUi.toolGroup.symphony.command' };
  }

  const baseValues = { target: action.target };
  const prefix =
    action.targetKind === 'skill'
      ? 'chatUi.toolGroup.symphony.readSkill'
      : action.targetKind === 'metadata'
        ? 'chatUi.toolGroup.symphony.readMetadata'
        : 'chatUi.toolGroup.symphony.readFile';
  if (action.lastLines) {
    return { key: `${prefix}Last`, values: { ...baseValues, count: action.lastLines } };
  }
  if (action.startLine && action.endLine) {
    const count = Math.max(0, action.endLine - action.startLine + 1);
    if (action.startLine === 1) {
      return {
        key: `${prefix}First`,
        values: { ...baseValues, end: action.endLine, count },
      };
    }
    return {
      key: `${prefix}Range`,
      values: {
        ...baseValues,
        start: action.startLine,
        end: action.endLine,
        count,
      },
    };
  }
  if (action.startLine) {
    return { key: `${prefix}From`, values: { ...baseValues, start: action.startLine } };
  }
  return { key: prefix, values: baseValues };
}

/**
 * Model tokenizers vary and are not shipped to the browser. Count lexical
 * words instead: Latin-like runs count once and each CJK character counts once.
 */
export function countResultWords(value: string): number {
  const text = String(value || '').trim();
  if (!text) {
    return 0;
  }
  const cjkPattern = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/gu;
  const cjkCount = text.match(cjkPattern)?.length || 0;
  const remainder = text.replace(cjkPattern, ' ');
  const wordCount = remainder.match(/[\p{L}\p{N}]+(?:[’'._-][\p{L}\p{N}]+)*/gu)?.length || 0;
  return cjkCount + wordCount;
}
