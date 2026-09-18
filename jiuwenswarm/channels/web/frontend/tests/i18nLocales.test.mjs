/**
 * 登录与免费模型相关文案的结构性校验。
 *
 * 1. locale 文件任意层级都不允许重复键：JSON 允许重复键、后者静默覆盖前者，
 *    合并分支时往文件末尾追加整段就会把原有文案整段换掉，JSON.parse 和 tsc 都看不出来。
 * 2. 本功能的文案（`auth.*`、`settingsPanel.freeModels.*`、`chat.modelSelector.*`）zh / en 一一对应，
 *    代码里静态引用到的这些键都存在——缺了界面会直接显示 key 原文。
 *    只查本功能自己的命名空间，别的模块的缺失不该让这里失败。
 */

import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const LOCALES_DIR = path.join(ROOT, 'src', 'i18n', 'locales');
const SRC_DIR = path.join(ROOT, 'src');

/** 本功能负责的文案命名空间。 */
const OWNED_PREFIXES = ['auth.', 'settingsPanel.freeModels', 'chat.modelSelector'];
const isOwned = (key) => OWNED_PREFIXES.some((prefix) => key === prefix || key.startsWith(prefix));

/**
 * 找出 JSON 文本里的重复键。
 *
 * 必须自己扫，不能用 JSON.parse —— 它在构造对象的那一刻就把重复键合并掉了，
 * 事后再怎么检查都看不出来。这里做一次最小的词法扫描，按对象嵌套深度记录
 * 每一层出现过的键名。
 *
 * @param {string} text JSON 原文
 * @returns {string[]} 形如 `auth`、`settingsPanel.models.addModel` 的重复键路径
 */
function findDuplicateKeys(text) {
  const duplicates = [];
  /** @type {Array<{ seen: Set<string>, key: string | null, isArray: boolean }>} */
  const stack = [];
  /** 最近一次读到的字符串字面量，可能是键也可能是值 */
  let lastString = null;
  let i = 0;

  const pathOf = () =>
    stack
      .map((frame) => frame.key)
      .filter((key) => key !== null)
      .join('.');

  while (i < text.length) {
    const ch = text[i];

    if (ch === '"') {
      let j = i + 1;
      let value = '';
      while (j < text.length) {
        if (text[j] === '\\') {
          value += text[j + 1];
          j += 2;
          continue;
        }
        if (text[j] === '"') break;
        value += text[j];
        j += 1;
      }
      lastString = value;
      i = j + 1;
      continue;
    }

    if (ch === ':') {
      // 上一个字符串是键名。记进当前对象层，重复就报。
      const frame = stack[stack.length - 1];
      if (frame && !frame.isArray && lastString !== null) {
        const prefix = pathOf();
        const full = prefix ? `${prefix}.${lastString}` : lastString;
        if (frame.seen.has(lastString)) duplicates.push(full);
        frame.seen.add(lastString);
        frame.key = lastString;
      }
      lastString = null;
      i += 1;
      continue;
    }

    if (ch === '{' || ch === '[') {
      stack.push({ seen: new Set(), key: null, isArray: ch === '[' });
      lastString = null;
      i += 1;
      continue;
    }

    if (ch === '}' || ch === ']') {
      stack.pop();
      lastString = null;
      i += 1;
      continue;
    }

    if (ch === ',') {
      const frame = stack[stack.length - 1];
      if (frame) frame.key = null;
      lastString = null;
      i += 1;
      continue;
    }

    i += 1;
  }
  return duplicates;
}

/** 把嵌套对象拍平成 `a.b.c` 形式的键集合（含中间节点）。 */
function flattenKeys(value, prefix = '', out = new Set()) {
  for (const [key, child] of Object.entries(value)) {
    const full = prefix ? `${prefix}.${key}` : key;
    out.add(full);
    if (child && typeof child === 'object' && !Array.isArray(child)) {
      flattenKeys(child, full, out);
    }
  }
  return out;
}

function listSourceFiles(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) listSourceFiles(full, out);
    else if (/\.tsx?$/.test(entry.name)) out.push(full);
  }
  return out;
}

const localeFiles = readdirSync(LOCALES_DIR).filter((name) => name.endsWith('.json'));
const locales = Object.fromEntries(
  localeFiles.map((name) => [
    name.replace(/\.json$/, ''),
    readFileSync(path.join(LOCALES_DIR, name), 'utf8'),
  ]),
);

test('locale 文件里没有重复键', () => {
  assert.ok(localeFiles.length >= 2, `至少应有 zh/en 两份，实际: ${localeFiles.join(', ')}`);
  for (const [name, text] of Object.entries(locales)) {
    const duplicates = findDuplicateKeys(text);
    assert.deepEqual(
      duplicates,
      [],
      `${name}.json 存在重复键：${duplicates.join(', ')}。` +
        ' JSON 允许重复键且后者静默覆盖前者——被覆盖的那份文案会直接消失。' +
        ' 合并分支时往文件末尾追加整段是最常见的成因；追加前先确认该顶层键不存在。',
    );
  }
});

test('重复键扫描本身是有效的（防止检查退化成永远通过）', () => {
  const withDuplicate =
    '{ "auth": { "title": "a" }, "other": 1, "auth": { "title": "b" } }';
  assert.deepEqual(findDuplicateKeys(withDuplicate), ['auth']);

  const nested = '{ "a": { "b": 1, "b": 2 } }';
  assert.deepEqual(findDuplicateKeys(nested), ['a.b']);

  // 不同对象下的同名键是完全正常的，不该误报
  assert.deepEqual(findDuplicateKeys('{ "a": { "x": 1 }, "b": { "x": 2 } }'), []);
  // 值里出现的冒号、花括号不能干扰扫描
  assert.deepEqual(findDuplicateKeys('{ "a": "{\\"x\\": 1}", "b": "y: z" }'), []);
});

test('本功能的文案 zh 与 en 键结构一致', () => {
  const zh = flattenKeys(JSON.parse(locales.zh));
  const en = flattenKeys(JSON.parse(locales.en));
  const onlyZh = [...zh].filter((key) => isOwned(key) && !en.has(key)).sort();
  const onlyEn = [...en].filter((key) => isOwned(key) && !zh.has(key)).sort();
  assert.deepEqual(
    { onlyZh, onlyEn },
    { onlyZh: [], onlyEn: [] },
    '两份文案的键必须一一对应；缺失的那边会把 key 原文直接显示给用户。',
  );
});

test('代码里静态引用的本功能文案都存在', () => {
  const zh = flattenKeys(JSON.parse(locales.zh));
  const en = flattenKeys(JSON.parse(locales.en));
  const missing = new Set();

  for (const file of listSourceFiles(SRC_DIR)) {
    const text = readFileSync(file, 'utf8');
    // 只认静态字面量 t('a.b')。模板串 t(`a.${x}`) 由运行时拼出来，静态查不了，
    // 硬查只会产生假阳性，所以跳过。
    for (const match of text.matchAll(/\bt\(\s*'([A-Za-z0-9_.]+)'/g)) {
      const key = match[1];
      if (!isOwned(key)) continue;
      if (!zh.has(key) || !en.has(key)) {
        missing.add(`${path.relative(ROOT, file)}: ${key}`);
      }
    }
  }

  const sorted = [...missing].sort();
  assert.deepEqual(
    sorted,
    [],
    '这些 key 在代码里被引用，但 zh/en 里没有——界面上会直接显示 key 原文：\n' +
      sorted.join('\n'),
  );
});
