import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';

import {
  isComposerSlashChipCommand,
  splitComposerSlashChip,
  createComposerSkillRange,
  readComposerSkillPositions,
  readComposerText,
} from '../node_modules/.cache/composer-draft-hydrate/composerDraftHydrate.mjs';

test('only parameterized draft slash commands become chips (#5771)', () => {
  assert.equal(isComposerSlashChipCommand('persist'), true);
  assert.equal(isComposerSlashChipCommand('Persist'), true);
  assert.equal(isComposerSlashChipCommand('plan'), false);
  assert.equal(isComposerSlashChipCommand('fork'), false);
});

for (const parts of [
  ['hi 使用最新的', { skill: 'swarmskill-creator' }],
  [{ skill: 'swarmskill-creator' }, 'hi 使用最新的'],
  ['前文', { skill: 'first' }, '后文'],
  ['前文', { skill: 'first' }, { skill: 'second' }, '后文'],
  [{ skill: 'first' }, { skill: 'second' }],
  ['/persist ', { skill: 'first' }, '🙂 中文', { skill: 'second' }],
]) {
  test(`session hydration preserves skill order: ${JSON.stringify(parts)}`, () => {
    const dom = new JSDOM('<div></div>');
    const editor = dom.window.document.querySelector('div');
    const createChip = (name) => {
      const chip = dom.window.document.createElement('span');
      chip.dataset.skill = name;
      chip.setAttribute('contenteditable', 'false');
      chip.textContent = name;
      return chip;
    };
    const order = () => Array.from(editor.childNodes).flatMap((node) => {
      if (node.dataset?.skill) return [{ skill: node.dataset.skill }];
      const text = (node.textContent || '').replace(/\u200B/g, '');
      return text ? [text] : [];
    });
    try {
      for (const part of parts) editor.append(typeof part === 'string' ? part : createChip(part.skill));
      for (let switchCount = 0; switchCount < 3; switchCount += 1) {
        const positions = readComposerSkillPositions(editor);
        const text = readComposerText(editor);
        editor.textContent = text;
        for (const { name, offset } of positions) {
          const chip = createChip(name);
          createComposerSkillRange(editor, offset).insertNode(chip);
          chip.after(dom.window.document.createTextNode('\u200B'));
        }
        assert.deepEqual(order(), parts);
        assert.equal(readComposerText(editor), text);
      }
    } finally {
      dom.window.close();
    }
  });
}

test('skill positions count slash and mention tokens as submitted text', () => {
  const dom = new JSDOM('<div><span contenteditable="false" data-slash-command="persist">persist ×</span> '
    + '<span contenteditable="false" data-composer-token="role" data-value="writer">Writer ×</span>'
    + '<span contenteditable="false" data-skill="demo">demo ×</span> end</div>');
  try {
    const editor = dom.window.document.querySelector('div');
    assert.equal(readComposerText(editor), '/persist $writer end');
    assert.deepEqual(readComposerSkillPositions(editor), [{ name: 'demo', offset: 16 }]);
    editor.querySelector('[data-skill]').remove();
    createComposerSkillRange(editor, 16).insertNode(dom.window.document.createTextNode('[skill]'));
    assert.equal(readComposerText(editor), '/persist $writer[skill] end');
  } finally {
    dom.window.close();
  }
});

test('nested editable lines keep skill labels out of plain text and restore positions', () => {
  const dom = new JSDOM('<div id="editor">before<div>line '
    + '<span contenteditable="false" data-skill="demo">demo ×</span>after</div></div>');
  try {
    const editor = dom.window.document.querySelector('#editor');
    assert.equal(readComposerText(editor), 'beforeline after');
    assert.deepEqual(readComposerSkillPositions(editor), [{ name: 'demo', offset: 11 }]);
    editor.querySelector('[data-skill]').remove();
    createComposerSkillRange(editor, 11).insertNode(dom.window.document.createTextNode('[skill]'));
    assert.equal(readComposerText(editor), 'beforeline [skill]after');
  } finally {
    dom.window.close();
  }
});

test('splitComposerSlashChip restores /persist chip + remaining args', () => {
  assert.deepEqual(splitComposerSlashChip('/persist follow release'), {
    command: 'persist',
    rest: ' follow release',
  });
  assert.deepEqual(splitComposerSlashChip('/persist'), {
    command: 'persist',
    rest: '',
  });
  assert.deepEqual(splitComposerSlashChip('/plan do stuff'), {
    command: null,
    rest: '/plan do stuff',
  });
  assert.deepEqual(splitComposerSlashChip('hello /persist'), {
    command: null,
    rest: 'hello /persist',
  });
});
