import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import ts from 'typescript';
import { loadCallback } from './loadProductionCallback.mjs';
import {
  NEW_CONVERSATION_ID,
  createComposerSkillRange,
  readComposerSkillPositions,
  readComposerText,
  resetNewConversationRuntime,
  useChatStore,
  useSessionStore,
} from '../node_modules/.cache/new-conversation-draft-preserve/newConversationDraftPreserveExports.mjs';

// Register the production App effect so the test also checks event forwarding.
function registerAppListener(bindings) {
  const source = ts.createSourceFile('App.tsx', readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8'),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let effect;
  function visit(node) {
    if (ts.isCallExpression(node) && node.expression.getText(source) === 'useEffect'
      && node.arguments[0]?.getText(source).includes("window.addEventListener('jiuwen:new-conversation'")) {
      effect = node.arguments[0].getText(source);
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(effect, 'App must register the skill navigation listener');
  const { outputText } = ts.transpileModule(`const effect = ${effect};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  });
  return new Function(...Object.keys(bindings), `${outputText}\nreturn effect();`)(...Object.values(bindings));
}

function createHarness(text = '请保留这段话', skill = 'agent-creator') {
  const previousChat = useChatStore.getState();
  const previousSession = useSessionStore.getState();
  const dom = new JSDOM('<div contenteditable="true"></div>');
  const { window } = dom;
  const { document } = window;
  const editor = document.querySelector('div');
  const scheduled = [];
  const chat = useChatStore.getState();
  const session = useSessionStore.getState();
  chat.removeRuntime('new');
  session.removeRuntime('new');
  chat.ensureRuntime('new');
  session.ensureRuntime('new');
  chat.setActiveSessionId('new');
  editor.textContent = text;
  if (skill) {
    const chip = document.createElement('span');
    chip.dataset.skill = skill;
    chip.setAttribute('contenteditable', 'false');
    chip.textContent = skill;
    editor.prepend(chip);
    session.addSelectedSkill('new', skill);
  }
  chat.setInputValue('new', text, readComposerSkillPositions(editor));
  const inputRef = { current: editor };
  const savedRangeRef = { current: null };
  const bindings = {
    inputRef, savedRangeRef, window, document,
    useChatStore, useSessionStore, readComposerText, createComposerSkillRange,
    extractPlainText: () => readComposerText(editor),
    isComposingRef: { current: false },
    isAgentGroupSelected: () => false,
    closeSvg: '',
  };
  const insertSkillChip = loadCallback('components/ChatPanel/InputArea.tsx', 'insertSkillChip', bindings);
  const handleInsertion = loadCallback('components/ChatPanel/InputArea.tsx', 'handleInsertSkillFromPage', {
    ...bindings, insertSkillChip,
  });
  window.addEventListener('chat-input-insert-skill', handleInsertion);
  const cleanupApp = registerAppListener({
    window, CustomEvent: window.CustomEvent, useSessionStore, NEW_CONVERSATION_ID,
    setTimeout: (callback) => scheduled.push(callback),
    enterNewConversation: (mode, options) => {
      resetNewConversationRuntime({ mode: options.forceMode ?? mode ?? 'agent', selectedModelName: null });
      const runtime = chat.getRuntime('new');
      editor.textContent = runtime.inputValue;
      for (const { name, offset } of runtime.inputSkillPositions) {
        const chip = document.createElement('span');
        chip.dataset.skill = name;
        chip.setAttribute('contenteditable', 'false');
        chip.textContent = name;
        createComposerSkillRange(editor, offset).insertNode(chip);
      }
      // Even a leftover selection must not replace text on an additive handoff.
      const range = document.createRange();
      range.selectNodeContents(editor);
      savedRangeRef.current = range.cloneRange();
    },
  });
  const goTry = loadCallback('components/SkillPanel/index.tsx', 'handleGoToChat', {
    window, CustomEvent: window.CustomEvent,
  });
  return {
    editor, chat, session,
    skills: () => Array.from(editor.querySelectorAll('[data-skill]'), (chip) => chip.dataset.skill),
    trySkill: (name, type) => {
      goTry(name, type);
      while (scheduled.length) scheduled.shift()();
    },
    insertPrompt: (detail) => handleInsertion(new window.CustomEvent('chat-input-insert-skill', { detail })),
    cleanup: () => {
      cleanupApp();
      dom.window.close();
      useChatStore.setState(previousChat, true);
      useSessionStore.setState(previousSession, true);
    },
  };
}

test('My Skills try preserves draft text and the existing skill through App navigation', () => {
  const harness = createHarness();
  try {
    harness.trySkill('swarmskill-creator');
    assert.equal(readComposerText(harness.editor), '请保留这段话');
    assert.equal(harness.chat.getRuntime('new').inputValue, '请保留这段话');
    assert.deepEqual(harness.skills(), ['agent-creator', 'swarmskill-creator']);
    assert.deepEqual(harness.session.getRuntime('new').selectedSkills, ['agent-creator', 'swarmskill-creator']);
    assert.equal(harness.editor.lastElementChild.dataset.skill, 'swarmskill-creator');
  } finally {
    harness.cleanup();
  }
});

test('trying the same skill again preserves text without duplicating its chip', () => {
  const harness = createHarness();
  try {
    harness.trySkill('agent-creator');
    assert.equal(readComposerText(harness.editor), '请保留这段话');
    assert.deepEqual(harness.skills(), ['agent-creator']);
  } finally {
    harness.cleanup();
  }
});

test('trying a skill preserves a text-only draft and its position after another reset', () => {
  const harness = createHarness('未发送的文字', null);
  try {
    harness.trySkill('demo-skill');
    assert.equal(readComposerText(harness.editor), '未发送的文字');
    assert.equal(harness.chat.getRuntime('new').inputValue, '未发送的文字');
    assert.deepEqual(readComposerSkillPositions(harness.editor), [{ name: 'demo-skill', offset: 6 }]);
    harness.chat.setInputValue('new', readComposerText(harness.editor), readComposerSkillPositions(harness.editor));
    resetNewConversationRuntime({ mode: 'agent', selectedModelName: null });
    assert.deepEqual(harness.chat.getRuntime('new').inputSkillPositions, [{ name: 'demo-skill', offset: 6 }]);
    assert.equal(harness.chat.getRuntime('new').inputValue, '未发送的文字');
  } finally {
    harness.cleanup();
  }
});

test('trying a skill with an empty draft still inserts a chip and sets its requested mode', () => {
  const harness = createHarness('', null);
  try {
    harness.trySkill('team-skill', 'swarm_skill');
    assert.equal(readComposerText(harness.editor), '');
    assert.deepEqual(harness.skills(), ['team-skill']);
    assert.equal(harness.session.getRuntime('new').mode, 'team');
  } finally {
    harness.cleanup();
  }
});

for (const target of [undefined, 'target-skill']) {
  test(`explicit skill creation/editing prompt still replaces the editor (target=${target})`, () => {
    const harness = createHarness();
    try {
      harness.insertPrompt({ skillName: 'skill-creator', suffixText: '帮我处理这个技能', secondSkillName: target });
      assert.equal(readComposerText(harness.editor), '帮我处理这个技能');
      assert.equal(harness.chat.getRuntime('new').inputValue, '帮我处理这个技能');
      assert.deepEqual(harness.skills(), target ? ['skill-creator', target] : ['skill-creator']);
    } finally {
      harness.cleanup();
    }
  });
}
