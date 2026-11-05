import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const componentCases = JSON.parse(readFileSync(new URL('./fixtures/webuiComponentCases.json', import.meta.url), 'utf8'));
const iconWhitelist = JSON.parse(readFileSync(new URL('../src/features/webui/icons/whitelist.json', import.meta.url), 'utf8'));
import {
  checkPropValue,
  componentCatalog,
  createCatalog,
  DEFAULT_WEBUI_LIMITS,
  WEBUI_COMPONENT_EVENTS,
  WEBUI_COMPONENT_SCHEMA_VERSION,
} from '../node_modules/.cache/webui-contracts/contracts.mjs';

// Exercise property rules independently of node traversal.
function checkProps(name, input = {}) {
  const rule = componentCatalog.getComponent(name);
  assert.ok(rule, `missing component: ${name}`);
  const props = { ...rule.defaults, ...input };
  const shape = { kind: 'object', fields: rule.props, required: rule.required };
  const error = checkPropValue(shape, props, DEFAULT_WEBUI_LIMITS) ?? rule.validate?.(props);
  return error ? { error } : { value: rule.prepare ? rule.prepare(props) : props };
}

test('catalog contains the supported components', () => {
  assert.deepEqual(componentCatalog.listComponents().map((rule) => rule.name).sort(), [
    'Avatar', 'Box', 'Button', 'Card', 'Divider', 'Dropdown', 'Empty', 'FilePreview', 'FileTree', 'Form', 'Form.Item', 'Icon',
    'Iframe', 'Image', 'Input', 'Panel', 'Select', 'Slider', 'Switch', 'Tabs', 'Tag', 'Text', 'TreeSelect',
  ].sort());
  assert.deepEqual(componentCatalog.getSlot('application.entry'), {
    id: 'application.entry',
    display: 'single',
    scope: 'application',
  });
  assert.deepEqual(componentCatalog.getSlot('application.sidebar.footer'), {
    id: 'application.sidebar.footer',
    display: 'single',
    scope: 'application',
  });
  assert.deepEqual(componentCatalog.getSlot('conversation.panel.right').rootComponents, ['Panel', 'Iframe']);
  assert.deepEqual(componentCatalog.getSlot('application.menu.main').rootComponents, ['Iframe']);
  assert.equal(WEBUI_COMPONENT_SCHEMA_VERSION, 1);
});

test('catalog rejects unsupported events', () => {
  const create = (rule) => createCatalog({ components: [rule], slots: [] });
  assert.throws(() => create({ name: 'UnknownEvent', props: {}, allowChildren: false, events: { 'ui.custom': {} } }),
    /unsupported event/);
});

test('Icon and icon-bearing components accept every whitelist icon and alias, and reject unsupported names', () => {
  const names = [...Object.keys(iconWhitelist.icons), ...Object.keys(iconWhitelist.aliases)];
  assert.deepEqual(componentCatalog.getComponent('Icon').props.name.values, names);
  for (const icon of names) assert.equal(checkProps('Icon', { name: icon }).error, undefined, icon);
  for (const icon of ['not-an-icon', 'Camera', 'constructor', '__proto__', '', null]) {
    assert.equal(typeof checkProps('Icon', { name: icon }).error, 'string', String(icon));
  }
  for (const component of ['Button', 'Dropdown']) {
    assert.deepEqual(componentCatalog.getComponent(component).props.icon.values, names);
    const props = component === 'Button' ? { 'aria-label': 'Action' }
      : { items: [{ key: 'action', label: 'Action' }], ariaLabel: 'Actions' };
    for (const icon of names) assert.equal(checkProps(component, { ...props, icon }).error, undefined, icon);
    for (const icon of ['not-an-icon', 'Camera', 'constructor', '__proto__', '', null]) {
      assert.equal(typeof checkProps(component, { ...props, icon }).error, 'string', String(icon));
    }
  }
});

test('Icon keeps its leaf contract and rejects blank accessible labels', () => {
  const rule = componentCatalog.getComponent('Icon');
  assert.equal(rule.allowChildren, false);
  assert.equal(rule.events, undefined);
  assert.deepEqual(rule.defaults, undefined);
  assert.equal(checkProps('Icon', { name: 'search', 'aria-label': 'Search' }).error, undefined);
  assert.equal(checkProps('Icon', { name: 'search', 'aria-label': '  ' }).error, 'icon label must be non-empty');
  assert.equal(checkProps('Icon', { name: 'search', 'aria-label': '' }).error, 'icon label must be non-empty');
  assert.equal(typeof checkProps('Icon', {}).error, 'string');
});

test('Box keeps source layout defaults and supports column direction', () => {
  assert.deepEqual(checkProps('Box').value, {
    direction: 'row', gap: 'md', align: 'stretch', justify: 'start', wrap: false,
  });
  assert.equal(checkProps('Box', { direction: 'column', gap: 'none', flex: 2 }).error, undefined);
  assert.equal(componentCatalog.getComponent('Box').allowChildren, true);
  assert.equal(componentCatalog.getComponent('Box').events, undefined);
});

const invalid = componentCases.invalid;

for (const [index, [name, props]] of invalid.entries()) {
  test(`rejects invalid ${name} props (${index + 1})`, () => {
    assert.equal(typeof checkProps(name, props).error, 'string', JSON.stringify({ name, props }));
  });
}

const valid = componentCases.valid;

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
  ['Button', 'ui.click', {}, { value: 12 }],
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
  assert.deepEqual(Object.keys(componentCatalog.getComponent('Select').events), ['ui.change', 'ui.select', 'ui.search', 'ui.clear']);
  assert.equal(componentCatalog.getComponent('Input').events['ui.clear'], undefined);
  assert.equal(componentCatalog.getComponent('Iframe').events, undefined);
  assert.deepEqual(componentCatalog.getComponent('Divider').childComponents, ['Text']);
  assert.deepEqual(componentCatalog.getComponent('Tabs').content, { prop: 'items', children: 'children' });
});

test('size aliases normalize consistently and compact components reject large sizes', () => {
  const cases = [
    ['Text', {}], ['Button', {}], ['Input', {}], ['Select', { options: [] }],
    ['TreeSelect', { treeData: [] }], ['Tabs', { items: [] }], ['Divider', {}], ['Avatar', { src: '/logo.svg' }],
    ['Icon', { name: 'search' }],
  ];
  for (const [name, props] of cases) {
    for (const [alias, canonical] of [['sm', 'small'], ['md', 'medium'], ['lg', 'large']]) {
      assert.equal(checkProps(name, { ...props, size: alias }).value.size, canonical, name);
      assert.equal(checkProps(name, { ...props, size: canonical }).value.size, canonical, name);
    }
  }
  for (const name of ['Card', 'Switch']) {
    assert.equal(checkProps(name, { size: 'sm' }).value.size, 'small');
    assert.equal(checkProps(name, { size: 'md' }).value.size, 'medium');
    for (const size of ['lg', 'large']) assert.ok(checkProps(name, { size }).error);
  }
  assert.equal(checkProps('Avatar', { src: '/logo.svg', size: 'default' }).value.size, 'medium');
  assert.equal(checkProps('Avatar', { src: '/logo.svg', size: 48 }).value.size, 48);
});
