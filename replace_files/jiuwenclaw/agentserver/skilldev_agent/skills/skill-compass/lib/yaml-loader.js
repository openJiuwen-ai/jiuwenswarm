let jsYaml = null;

try {
  jsYaml = require('js-yaml');
} catch (_error) {
  jsYaml = null;
}

function load(source) {
  if (jsYaml) {
    return jsYaml.load(source);
  }
  return parseMapping(source.split(/\r?\n/), 0, 0).value;
}

function parseMapping(lines, startIndex, indent) {
  const result = {};
  let index = startIndex;

  while (index < lines.length) {
    const raw = lines[index];
    if (!raw.trim() || raw.trimStart().startsWith('#')) {
      index += 1;
      continue;
    }

    const currentIndent = countIndent(raw);
    if (currentIndent < indent) break;
    if (currentIndent > indent) {
      index += 1;
      continue;
    }

    const line = raw.slice(indent);
    const match = line.match(/^([A-Za-z0-9_-]+):(?:\s*(.*))?$/);
    if (!match) {
      index += 1;
      continue;
    }

    const key = match[1];
    const rest = (match[2] || '').trim();
    if (rest === '|' || rest === '>') {
      const block = collectBlock(lines, index + 1, indent + 2, rest === '>');
      result[key] = block.value;
      index = block.index;
      continue;
    }
    if (rest) {
      result[key] = parseScalar(rest);
      index += 1;
      continue;
    }

    const nested = parseNested(lines, index + 1, indent + 2);
    result[key] = nested.value;
    index = nested.index;
  }

  return { value: result, index };
}

function parseNested(lines, startIndex, indent) {
  let index = startIndex;
  while (index < lines.length && !lines[index].trim()) index += 1;
  if (index >= lines.length || countIndent(lines[index]) < indent) {
    return { value: null, index };
  }
  if (lines[index].slice(indent).startsWith('- ')) {
    return parseArray(lines, index, indent);
  }
  return parseMapping(lines, index, indent);
}

function parseArray(lines, startIndex, indent) {
  const result = [];
  let index = startIndex;

  while (index < lines.length) {
    const raw = lines[index];
    if (!raw.trim()) {
      index += 1;
      continue;
    }
    const currentIndent = countIndent(raw);
    if (currentIndent < indent) break;
    if (currentIndent !== indent || !raw.slice(indent).startsWith('- ')) break;

    const itemText = raw.slice(indent + 2).trim();
    if (!itemText) {
      const nested = parseNested(lines, index + 1, indent + 2);
      result.push(nested.value);
      index = nested.index;
      continue;
    }

    const objectMatch = itemText.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
    if (objectMatch) {
      const item = {};
      item[objectMatch[1]] = parseScalar(objectMatch[2]);
      index += 1;
      while (index < lines.length && countIndent(lines[index]) === indent + 2) {
        const nestedLine = lines[index].slice(indent + 2);
        const nestedMatch = nestedLine.match(/^([A-Za-z0-9_-]+):\s*(.*)$/);
        if (!nestedMatch) break;
        item[nestedMatch[1]] = parseScalar(nestedMatch[2]);
        index += 1;
      }
      result.push(item);
      continue;
    }

    result.push(parseScalar(itemText));
    index += 1;
  }

  return { value: result, index };
}

function collectBlock(lines, startIndex, indent, fold) {
  const blockLines = [];
  let index = startIndex;
  while (index < lines.length) {
    const raw = lines[index];
    if (raw.trim() && countIndent(raw) < indent) break;
    blockLines.push(raw.length >= indent ? raw.slice(indent) : '');
    index += 1;
  }
  return {
    value: fold ? blockLines.join(' ').replace(/\s+/g, ' ').trim() : blockLines.join('\n'),
    index
  };
}

function parseScalar(value) {
  const trimmed = value.trim();
  if (!trimmed) return '';
  if ((trimmed.startsWith('"') && trimmed.endsWith('"')) ||
      (trimmed.startsWith("'") && trimmed.endsWith("'"))) {
    return trimmed.slice(1, -1);
  }
  if (trimmed === 'true') return true;
  if (trimmed === 'false') return false;
  if (trimmed === 'null') return null;
  if (/^-?\d+(?:\.\d+)?$/.test(trimmed)) return Number(trimmed);
  return trimmed;
}

function countIndent(line) {
  const match = line.match(/^ */);
  return match ? match[0].length : 0;
}

module.exports = { load };
