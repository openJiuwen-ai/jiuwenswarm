import { array, bool, component, integer, nonEmpty, object, text, token } from './shared.ts';

const rule = object({ required: bool, min: integer(), max: integer(), pattern: text, message: text });
export const formRule = component('Form', { name: text, requiredMark: bool,
  layout: token('vertical', 'horizontal', 'inline'), disabled: bool, ariaLabel: text }, {
  required: ['name'], allowChildren: true, childComponents: ['Form.Item'],
  validate: (props) => nonEmpty(props.name) ? null : 'Form.name must not be empty',
});
export const formItemRule = component('Form.Item', { name: text, label: text, rules: array(rule) }, {
  required: ['name'], allowChildren: true, childComponents: ['Input'],
  validate: (props) => {
    if (!nonEmpty(props.name) || ['__proto__', 'constructor', 'prototype'].includes(props.name as string)) {
      return 'invalid field name';
    }
    for (const item of (props.rules ?? []) as Record<string, unknown>[]) {
      if (item.min !== undefined && item.max !== undefined && (item.min as number) > (item.max as number)) {
        return 'minimum length exceeds maximum length';
      }
      if (typeof item.pattern === 'string') {
        try { new RegExp(item.pattern); } catch { return 'invalid regular expression'; }
      }
    }
    return null;
  },
});
