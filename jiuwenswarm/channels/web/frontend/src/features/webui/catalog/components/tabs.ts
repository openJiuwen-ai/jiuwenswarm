import { array, bool, component, object, size, text, token, unique } from './shared.ts';

export const tabsRule = component('Tabs', {
  items: array(object({ key: text, label: text, disabled: bool, children: { kind: 'nodes' } }, ['key', 'label'])),
  activeKey: text, defaultActiveKey: text, type: token('line', 'card'),
  tabPlacement: token('top', 'end', 'start', 'bottom'), size, centered: bool, destroyOnHidden: bool, ariaLabel: text,
}, {
  required: ['items'], defaults: { type: 'line', tabPlacement: 'top', size: 'medium', centered: false, destroyOnHidden: false },
  content: { prop: 'items', children: 'children' },
  events: { 'ui.change': object({ activeKey: text }, ['activeKey']) },
  validateEvent: (_event, data, p) => (p.items as { key: string; disabled?: boolean }[])
    .some((item) => item.key === data.activeKey && !item.disabled) ? null : 'invalid active tab',
  validate(p) {
    const items = p.items as { key: string; disabled?: boolean }[];
    if (!unique(items.map((item) => item.key)) || items.some((item) => !item.key)) return 'invalid tab keys';
    return ['activeKey', 'defaultActiveKey'].some((field) => p[field] !== undefined
      && !items.some((item) => item.key === p[field] && !item.disabled)) ? 'invalid active tab' : null;
  },
});
