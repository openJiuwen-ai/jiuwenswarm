import { bool, component, object, text, token } from './shared.ts';

const checkedData = object({ checked: bool }, ['checked']);
export const switchRule = component('Switch', {
  checked: bool, defaultChecked: bool, loading: bool, disabled: bool, ariaLabel: text,
  size: token('medium', 'small'), checkedChildren: text, unCheckedChildren: text,
}, {
  defaults: { defaultChecked: false, size: 'medium' },
  events: { 'ui.change': checkedData, 'ui.click': checkedData },
});
