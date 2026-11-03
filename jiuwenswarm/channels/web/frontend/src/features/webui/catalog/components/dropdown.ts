import { array, bool, component, object, text, token } from './shared.ts';

/** Menu shell: the trigger area is supplied as children, menu items stay a bounded array. */
export const dropdownRule = component('Dropdown', {
  items: array(object({
    key: text,
    label: text,
    disabled: bool,
    danger: bool,
  }, ['key', 'label'])),
  trigger: token('click', 'hover'),
  placement: token('bottomLeft', 'bottomRight', 'topLeft', 'topRight'),
  disabled: bool,
  ariaLabel: text,
  icon: token('ellipsis'),
}, {
  allowChildren: true,
  required: ['items'],
  defaults: { trigger: 'click', placement: 'topLeft', disabled: false },
  events: { 'ui.select': object({ key: text }, ['key']) },
  validate: (props) => {
    if (props.icon && !String(props.ariaLabel ?? '').trim()) return 'icon dropdowns require a non-empty ariaLabel';
    const items = props.items as Array<{ key: string; label: string }>;
    if (items.length === 0) return 'dropdown items must not be empty';
    if (new Set(items.map((item) => item.key)).size !== items.length) return 'dropdown item keys must be unique';
    return items.some((item) => item.label.trim().length === 0) ? 'dropdown item labels must not be empty' : null;
  },
  validateEvent: (_event, data, props) => {
    const items = props.items as Array<{ key: string; disabled?: boolean }>;
    const item = items.find((entry) => entry.key === data.key);
    return item && !item.disabled ? null : 'dropdown item is not selectable';
  },
});
