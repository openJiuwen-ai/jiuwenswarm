import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import ts from 'typescript';

// Execute the production effect with persistent refs and observable page setters.
const source = fs.readFileSync(new URL('../src/features/jiuwenmate/JiuwenMateApp.tsx', import.meta.url), 'utf8');
const tree = ts.createSourceFile('JiuwenMateApp.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let callback;
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(tree) === 'useEffect' &&
      node.arguments[1]?.getText(tree) === '[groupId, loadStatus]') {
    callback = node.arguments[0].getText(tree);
  }
  ts.forEachChild(node, visit);
}
visit(tree);
assert.ok(callback, 'group route effect exists');
const javascript = ts.transpile(`(${callback})`, { target: ts.ScriptTarget.ES2020 });

function fixture() {
  const activatedGroupIdRef = { current: undefined };
  const state = { page: null, rail: 'office', activations: 0 };
  return {
    state,
    render(groupId, loadStatus = 'ready') {
      const context = {
        groupId, loadStatus, activatedGroupIdRef,
        setExistingPage: (page) => { state.page = page; },
        setPublicLabel: () => {},
        setActiveRailView: (rail) => { state.rail = rail; },
        setState: () => { state.activations += 1; },
      };
      Function(...Object.keys(context), `return ${javascript}`)(...Object.values(context))();
    },
  };
}

for (const page of ['settings', 'personalContext', 'skills']) {
  test(`same-group list refresh preserves ${page}`, () => {
    const f = fixture();
    f.render('group-a');
    f.state.page = page;
    f.render('group-a', 'loading');
    f.render('group-a', 'ready');
    assert.equal(f.state.page, page);
    assert.equal(f.state.activations, 1);
  });
}

test('initial group waits for readiness and activates chat once', () => {
  const f = fixture();
  f.render('group-a', 'loading');
  assert.equal(f.state.activations, 0);
  f.render('group-a');
  assert.equal(f.state.rail, 'messages');
  assert.equal(f.state.activations, 1);
});

test('switching groups activates the new group after loading', () => {
  const f = fixture();
  f.render('group-a');
  f.state.page = 'skills';
  f.render('group-b', 'loading');
  assert.equal(f.state.page, 'skills');
  f.render('group-b');
  assert.equal(f.state.page, null);
  assert.equal(f.state.activations, 2);
});

test('leaving and reentering the same group activates chat again', () => {
  const f = fixture();
  f.render('group-a');
  f.render(undefined, 'loading');
  f.state.page = 'settings';
  f.render('group-a');
  assert.equal(f.state.page, null);
  assert.equal(f.state.activations, 2);
});
