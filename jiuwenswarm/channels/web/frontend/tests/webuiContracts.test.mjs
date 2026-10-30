import assert from 'node:assert/strict';
import test from 'node:test';
import {
  checkPropValue,
  componentCatalog,
  createCatalog,
  DEFAULT_WEBUI_LIMITS,
  WEBUI_COMPONENT_EVENTS,
  WEBUI_COMPONENT_SCHEMA_VERSION,
  WEBUI_SUPPORTED_SCHEMA_VERSION,
} from '../node_modules/.cache/webui-contracts/contracts.mjs';

// Exercise catalog rules independently; the production node walker is migrated later.
function checkProps(name, input = {}) {
  const rule = componentCatalog.getComponent(name);
  assert.ok(rule, `missing component: ${name}`);
  const props = { ...rule.defaults, ...input };
  const shape = { kind: 'object', fields: rule.props, required: rule.required };
  const error = checkPropValue(shape, props, DEFAULT_WEBUI_LIMITS) ?? rule.validate?.(props);
  return error ? { error } : { value: rule.prepare ? rule.prepare(props) : props };
}

test('catalog contains the source components except the deferred file components', () => {
  assert.deepEqual(componentCatalog.listComponents().map((rule) => rule.name).sort(), [
    'Box', 'Button', 'Card', 'Divider', 'Empty', 'Iframe', 'Image', 'Input',
    'Panel', 'Select', 'Slider', 'Switch', 'Tabs', 'Tag', 'Text', 'TreeSelect',
  ].sort());
  assert.equal(componentCatalog.getSlot('application.entry'), undefined);
  assert.deepEqual(componentCatalog.getSlot('conversation.panel.right').rootComponents, ['Panel', 'Iframe']);
  assert.deepEqual(componentCatalog.getSlot('application.menu.main').rootComponents, ['Iframe']);
  assert.equal(WEBUI_COMPONENT_SCHEMA_VERSION, 1);
  assert.equal(WEBUI_COMPONENT_SCHEMA_VERSION, WEBUI_SUPPORTED_SCHEMA_VERSION);
});

test('catalog rejects unsupported events and preserves existing action-rule checks', () => {
  const create = (rule) => createCatalog({ components: [rule], slots: [] });
  assert.throws(() => create({ name: 'UnknownEvent', props: {}, allowChildren: false, events: { 'ui.custom': {} } }),
    /unsupported event/);
  assert.throws(() => create({ name: 'MissingEvent', props: {}, allowChildren: false, allowAction: true }),
    /has no actionEvent/);
  assert.throws(() => create({ name: 'UnexpectedEvent', props: {}, allowChildren: false, actionEvent: 'ui.press' }),
    /does not allow action/);
});

test('Box keeps source layout defaults and supports column direction', () => {
  assert.deepEqual(checkProps('Box').value, {
    direction: 'row', gap: 'md', align: 'stretch', justify: 'start', wrap: false,
  });
  assert.equal(checkProps('Box', { direction: 'column', gap: 'none', flex: 2 }).error, undefined);
  assert.equal(componentCatalog.getComponent('Box').allowChildren, true);
  assert.equal(componentCatalog.getComponent('Box').events, undefined);
});

const invalid = [
  ['Box', { direction: 'diagonal' }], ['Box', { flex: -1 }], ['Box', { padding: 'sm' }],
  ['Box', { style: { color: 'red' } }], ['Text', { value: 'x'.repeat(16 * 1024 + 1) }],
  ['Input', { value: 1 }], ['Input', { maxLength: 1, defaultValue: 'long' }],
  ['Input', { value: 'valid', defaultValue: 8 }], ['Input', { variant: 'wrong' }], ['Input', { onChange: 'code' }],
  ['Switch', { checked: null }], ['Switch', { size: 'default' }],
  ['Select', {}], ['Select', { options: [{ value: 'a', label: 'A', render: true }] }],
  ['Select', { options: [{ value: 'a', label: 'A' }, { value: 2, label: 'B' }] }],
  ['Select', { options: [{ value: '', label: '' }] }], ['Select', { options: [], value: 'missing' }],
  ['Select', { options: [], mode: 'multiple', value: null }],
  ['Select', { options: [], mode: 'tags', value: [1] }],
  ['Select', { options: [], mode: 'tags', value: ['a', 'a'] }],
  ['Select', { options: [], mode: 'tags', maxCount: 1, value: ['a', 'b'] }],
  ['TreeSelect', { treeData: [{ value: 'a', title: 'A', children: [{ value: 'a', title: 'Again' }] }] }],
  ['TreeSelect', { treeData: [{ value: 'a', title: 'A', key: 'shared' }, { value: 'b', title: 'B', key: 'shared' }] }],
  ['TreeSelect', { treeData: [], treeCheckable: true, multiple: false }],
  ['TreeSelect', { treeData: [], treeDefaultExpandedKeys: ['missing'] }],
  ['TreeSelect', { treeData: [{ value: 'a', title: 'A' }], treeDefaultExpandAll: true, treeDefaultExpandedKeys: ['a'] }],
  ['Slider', { min: 10, max: 0 }], ['Slider', { step: 0 }], ['Slider', { step: 3 }], ['Slider', { value: 1.5 }],
  ['Slider', { dots: true, value: 50 }], ['Slider', { marks: { 101: 'bad' } }],
  ['Slider', { marks: { 1: 'one', '01': 'duplicate' } }], ['Slider', { range: true }],
  ['Tabs', { items: [{ key: 'a', label: 'A' }, { key: 'a', label: 'Again' }] }],
  ['Tabs', { items: [{ key: 'a', label: 'A', disabled: true }], activeKey: 'a' }],
  ['Image', { src: 'javascript:alert(1)' }], ['Image', { src: 'a.png', width: 'calc(100% - 1px)' }],
  ['Image', { src: 'a.png', width: '101%' }], ['Image', { src: 'a.png', width: 4097 }],
  ['Image', { src: 'a.png', preview: { src: 'a.png', onOpenChange: 'code' } }],
  ['Empty', { imageStyle: {} }], ['Empty', { image: 'data:image/png;base64,a' }],
  ['Card', { actions: [] }], ['Card', { children: [] }], ['Card', { extra: {} }],
  ['Divider', { orientation: 'vertical', children: 'Title' }],
  ['Tag', { color: '#ff0000' }], ['Tag', { href: 'java\nscript:alert(1)' }],
  ['Panel', { title: ' ' }], ['Iframe', { title: 'Preview', src: 'https://user:pass@example.com' }],
  ['Iframe', { title: 'Preview', src: '///example.com' }], ['Iframe', { src: 'https://example.com' }],
];

for (const [index, [name, props]] of invalid.entries()) {
  test(`rejects invalid ${name} props (${index + 1})`, () => {
    assert.equal(typeof checkProps(name, props).error, 'string', JSON.stringify({ name, props }));
  });
}

const valid = [
  ['Text', { value: 'hello' }], ['Button', { label: 'Confirm', disabled: true }],
  ['Input', { value: '', maxLength: 0 }], ['Switch', { checked: false, defaultChecked: true }],
  ['Select', { options: [], value: null }], ['Select', { options: [], mode: 'tags', value: ['custom'] }],
  ['TreeSelect', { treeData: [], multiple: true, value: [] }], ['Tabs', { items: [] }],
  ['Slider', { min: 0, max: 1, step: 0.1, value: 0.3 }],
  ['Slider', { step: 10, dots: true, marks: { 15: '15' }, value: 15 }],
  ['Image', { src: 'a.png', width: '100%', height: 4096 }], ['Empty', { image: 'simple' }],
  ['Card', { title: 'Title', cover: 'cover.png' }], ['Divider', { children: 'Title' }],
  ['Tag', { href: 'https://example.com', color: 'success' }], ['Panel', { title: 'Panel' }],
  ['Iframe', { title: 'Preview', src: '/preview' }],
];

for (const [index, [name, props]] of valid.entries()) {
  test(`accepts valid ${name} props (${index + 1})`, () => {
    assert.equal(checkProps(name, props).error, undefined, JSON.stringify({ name, props }));
  });
}

test('preparation preserves explicit false and empty values and uses source defaults', () => {
  assert.equal(checkProps('Switch', { checked: false, defaultChecked: true }).value.checked, false);
  assert.deepEqual(checkProps('TreeSelect', { treeData: [], multiple: true, value: [] }).value.value, []);
  assert.equal(checkProps('Select', { options: [], mode: 'tags' }).value.showSearch, true);
  assert.equal(checkProps('Select', { options: [], mode: 'tags', showSearch: false }).value.showSearch, false);
  assert.equal(checkProps('Slider', { min: -3, max: 3, step: 2 }).value.defaultValue, -1);
  assert.equal(checkProps('Slider', { min: 10, max: 20 }).value.defaultValue, 10);
});

test('TreeSelect accepts eight levels and rejects excessive depth and total option count', () => {
  let treeData = [{ value: 'leaf', title: 'Leaf' }];
  for (let i = 0; i < 7; i += 1) treeData = [{ value: `parent-${i}`, title: 'Parent', children: treeData }];
  assert.equal(checkProps('TreeSelect', { treeData }).error, undefined);
  assert.equal(typeof checkProps('TreeSelect', { treeData: [{ value: 'ninth', title: 'Ninth', children: treeData }] }).error,
    'string');
  const large = Array.from({ length: 6 }, (_, i) => ({ value: `p-${i}`, title: 'Parent', children:
    Array.from({ length: 90 }, (_, j) => ({ value: `${i}-${j}`, title: 'Child' })) }));
  assert.equal(typeof checkProps('TreeSelect', { treeData: large }).error, 'string');
});

const eventCases = [
  ['Button', 'ui.click', {}, { value: 'extra' }],
  ['Input', 'ui.change', { value: '北京', isComposing: false }, { value: '北京' }],
  ['Input', 'ui.press_enter', { value: '' }, { value: null }],
  ['Switch', 'ui.change', { checked: false }, { checked: 'false' }],
  ['Select', 'ui.select', { value: 'a' }, { value: ['a'] }],
  ['Select', 'ui.search', { searchValue: '' }, { value: '' }],
  ['Select', 'ui.clear', {}, { value: null }],
  ['TreeSelect', 'ui.change', { value: [] }, { value: 1 }],
  ['Slider', 'ui.change', { value: 0 }, { value: Infinity }],
  ['Tabs', 'ui.change', { activeKey: 'a' }, { key: 'a' }],
];

for (const [name, event, validData, invalidData] of eventCases) {
  test(`${name} ${event} validates declared data without dispatching`, () => {
    const rule = componentCatalog.getComponent(name).events[event];
    assert.equal(checkPropValue(rule, validData, DEFAULT_WEBUI_LIMITS), null);
    assert.equal(typeof checkPropValue(rule, invalidData, DEFAULT_WEBUI_LIMITS), 'string');
  });
}

test('component event sets retain the source restrictions', () => {
  assert.deepEqual(WEBUI_COMPONENT_EVENTS, ['ui.click', 'ui.change', 'ui.press_enter', 'ui.search', 'ui.clear', 'ui.select']);
  assert.deepEqual(Object.keys(componentCatalog.getComponent('Select').events), ['ui.select', 'ui.search', 'ui.clear']);
  assert.equal(componentCatalog.getComponent('Input').events['ui.clear'], undefined);
  assert.equal(componentCatalog.getComponent('Iframe').events, undefined);
  assert.deepEqual(componentCatalog.getComponent('Divider').childComponents, ['Text']);
  assert.deepEqual(componentCatalog.getComponent('Tabs').content, { prop: 'items', children: 'children' });
});
