import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import postcss from 'postcss';

const panelCss = postcss.parse(
  readFileSync(new URL('../src/components/SkillGraphPanel/SkillGraphPanel.css', import.meta.url), 'utf8'),
);

function findRule(root, selector) {
  let match;
  root.walkRules(selector, (rule) => {
    if (rule.parent?.type !== 'atrule') match = rule;
  });
  assert.ok(match, `Missing ${selector}`);
  return match;
}

function declarations(rule) {
  return Object.fromEntries(rule.nodes.filter((node) => node.type === 'decl').map((node) => [node.prop, node.value]));
}

test('short graph panels keep every sidebar section reachable', () => {
  const sidebar = declarations(findRule(panelCss, '.skill-graph-panel__sidebar'));
  assert.equal(sidebar['overflow-x'], 'hidden');
  assert.equal(sidebar['overflow-y'], 'auto');
  assert.equal(sidebar['scrollbar-gutter'], 'stable');

  assert.equal(declarations(findRule(panelCss, '.skill-graph-panel__sidebar > *'))['flex-shrink'], '0');

  const nodeList = declarations(findRule(panelCss, '.skill-graph-panel__node-list'));
  assert.equal(nodeList['min-height'], '180px');
  assert.equal(nodeList.flex, '1 0 180px');

  const nodeListItems = declarations(findRule(panelCss, '.skill-graph-panel__node-list-items'));
  assert.equal(nodeListItems.overflow, 'auto');
  assert.equal(nodeListItems['padding-right'], '8px');

  const buildLog = declarations(findRule(panelCss, '.skill-graph-panel__build-log'));
  assert.equal(buildLog['min-height'], '150px');
  assert.equal(buildLog['max-height'], '220px');

  const errorText = declarations(findRule(panelCss, '.skill-graph-panel__error span'));
  assert.equal(errorText['white-space'], 'normal');
  assert.equal(errorText['overflow-wrap'], 'anywhere');
});

test('narrow desktop graph pages preserve canvas space and wrap sidebar help', () => {
  assert.equal(
    declarations(findRule(panelCss, '.skill-graph-panel'))['grid-template-columns'],
    'clamp(300px, 30%, 400px) minmax(0, 1fr)',
  );

  const help = declarations(findRule(panelCss, '.skill-graph-panel__actions-help li'));
  assert.equal(help['overflow-wrap'], 'anywhere');
  assert.equal(help['white-space'], 'normal');
});
