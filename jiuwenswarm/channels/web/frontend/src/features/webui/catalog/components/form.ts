import { array, bool, component, integer, object, text, token } from './shared.ts';

const rule = object({ required: bool, min: integer(), max: integer(), pattern: text, message: text });
const fieldName = { kind: 'string', maxBytes: 200 } as const;
export const formRule = component('Form', { name: fieldName, requiredMark: bool,
  layout: token('vertical', 'horizontal', 'inline'), disabled: bool, ariaLabel: text }, {
  required: ['name'], allowChildren: true, childComponents: ['Form.Item'],
  validate: (props) => String(props.name).trim() ? null : 'Form.name must not be empty',
});
export const formItemRule = component('Form.Item', { name: fieldName, label: text, rules: array(rule) }, {
  required: ['name'], allowChildren: true, childComponents: ['Input'],
  validate: (props) => {
    if (!String(props.name).trim() || ['__proto__', 'constructor', 'prototype'].includes(props.name as string)) {
      return 'invalid field name';
    }
    for (const item of (props.rules ?? []) as Record<string, unknown>[]) {
      if (!['required', 'min', 'max', 'pattern'].some((key) => key in item)
        || (typeof item.message === 'string' && !item.message.trim())) return 'invalid form rule';
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
