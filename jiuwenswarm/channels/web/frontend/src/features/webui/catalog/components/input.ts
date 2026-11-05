import { bool, component, inputProps, integer, object, text, token, valueData } from './shared.ts';

export const inputRule = component('Input', {
  ...inputProps, value: text, defaultValue: text, placeholder: text, maxLength: integer(),
  allowClear: bool, showCount: bool, type: token('text', 'email', 'tel', 'url', 'password'), readOnly: bool,
  autoComplete: text,
}, {
  defaults: { defaultValue: '', size: 'medium', variant: 'outlined', type: 'text' },
  events: {
    'ui.click': valueData(text),
    'ui.change': object({ value: text, isComposing: bool }, ['value', 'isComposing']),
    'ui.press_enter': valueData(text),
  },
  validate: (p) => p.maxLength !== undefined && [p.value, p.defaultValue].some(
    (value) => typeof value === 'string' && value.length > (p.maxLength as number),
  ) ? 'initial text exceeds maxLength' : null,
  validateEvent: (event, data, p) => (p.readOnly && event !== 'ui.click')
    || (p.maxLength !== undefined && (data.value as string).length > (p.maxLength as number))
    ? 'input value is not allowed' : null,
});
