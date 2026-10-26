import assert from 'node:assert/strict';
import test from 'node:test';

const { filterEditableComboboxOptions, resolveEditableComboboxCommit } = await import(
  '../node_modules/.cache/editable-combobox/components/SessionSidebar/editableComboboxModel.js'
);

const options = [
  { value: 'group-alpha', label: '研发组织 · group-alpha' },
  { value: 'group-beta', label: '测试组织 · group-beta' },
];

test('empty query and selected label both expose the complete authorized list', () => {
  assert.deepEqual(filterEditableComboboxOptions(options, '', options[0].label), options);
  assert.deepEqual(filterEditableComboboxOptions(options, options[0].label, options[0].label), options);
});

test('editable query filters by display name or identifier without changing the source list', () => {
  assert.deepEqual(filterEditableComboboxOptions(options, '测试', options[0].label), [options[1]]);
  assert.deepEqual(filterEditableComboboxOptions(options, 'ALPHA', options[0].label), [options[0]]);
  assert.deepEqual(filterEditableComboboxOptions(options, 'missing', options[0].label), []);
  assert.equal(options.length, 2);
});

test('commit resolves an exact option but preserves an unknown debug value', () => {
  assert.equal(resolveEditableComboboxCommit(options, '测试组织 · group-beta'), 'group-beta');
  assert.equal(resolveEditableComboboxCommit(options, 'GROUP-ALPHA'), 'group-alpha');
  assert.equal(resolveEditableComboboxCommit(options, ' debug-group '), 'debug-group');
  assert.equal(resolveEditableComboboxCommit(options, '   '), null);
});
