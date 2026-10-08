import assert from 'node:assert/strict';
import test from 'node:test';
import { shellRulePattern } from '../node_modules/.cache/shell-rule-pattern/components/ConfigPanel/shellRulePattern.js';

test('editor rejects prefixes in either mode before constructing a save payload', () => {
  for (const mode of ['glob', 'regex']) {
    for (const pattern of ['re:^shutdown', ' re:re:^shutdown ', 'RE:^shutdown']) {
      const result = shellRulePattern(mode, pattern);
      assert.equal(result.error, 'shellSecurity.removeRegexPrefix');
      assert.equal(result.pattern, undefined);
    }
  }
});

test('editor preserves Python regex syntax and existing search semantics', () => {
  assert.deepEqual(shellRulePattern('regex', ' (?i)^echo\\s+hello$ '), { pattern: 're:(?i)^echo\\s+hello$' });
  assert.deepEqual(shellRulePattern('regex', 'echo'), { pattern: 're:echo' });
  assert.deepEqual(shellRulePattern('glob', ' echo ? '), { pattern: 'echo ?' });
});

test('editor rejects empty patterns', () => {
  for (const mode of ['glob', 'regex']) {
    assert.equal(shellRulePattern(mode, '  ').error, 'shellSecurity.patternRequired');
  }
});
