import {
  array, bool, component, inputProps, integer, nil, noData, nonEmpty, number, object,
  text, token, union, unique, valueData,
} from './shared.ts';

const optionValue = union(text, number);
import { localizedTextRule } from './localizedText.ts';
const selection = union(optionValue, nil, array(optionValue));
export const selectRule = component('Select', {
  ...inputProps, ariaLabel: localizedTextRule,
  options: array(object({ value: optionValue, label: localizedTextRule, disabled: bool }, ['value', 'label'])),
  value: selection, defaultValue: selection,
  placeholder: text, allowClear: bool, mode: token('multiple', 'tags'), maxCount: integer(1),
  maxTagCount: union(integer(), token('responsive')), showSearch: bool,
}, {
  required: ['options'], defaults: { size: 'medium', variant: 'outlined' },
  events: {
    'ui.change': valueData(selection),
    'ui.select': valueData(optionValue), 'ui.search': object({ searchValue: text }, ['searchValue']), 'ui.clear': noData,
  },
  validate(p) {
    const options = p.options as { value: string | number }[];
    const values = options.map((item) => item.value);
    const type = p.mode === 'tags' ? 'string' : typeof values[0];
    if (!unique(values) || values.some((value) => !nonEmpty(value) || typeof value !== type)) return 'invalid option values';
    for (const field of ['value', 'defaultValue']) {
      const value = p[field];
      if (value === undefined) continue;
      const multiple = p.mode !== undefined;
      if (multiple !== Array.isArray(value)) return 'selection does not match mode';
      const list = Array.isArray(value) ? value : value === null ? [] : [value];
      if (!unique(list) || (multiple && p.maxCount !== undefined && list.length > (p.maxCount as number))) return 'invalid selected count';
      if (list.some((item) => p.mode === 'tags' ? typeof item !== 'string' || !nonEmpty(item) : !values.includes(item))) {
        return 'selected value is not allowed';
      }
    }
    return null;
  },
  prepare: (p) => ({ ...p, showSearch: p.showSearch ?? p.mode !== undefined }),
  validateEvent(event, data, p) {
    if (event === 'ui.clear') return p.allowClear ? null : 'clearing is disabled';
    if (event === 'ui.search') return (p.showSearch ?? p.mode !== undefined) ? null : 'search is disabled';
    if (event === 'ui.change') {
      const multiple = p.mode !== undefined;
      if (multiple !== Array.isArray(data.value)) return 'selection does not match mode';
      if (data.value === null && !p.allowClear) return 'clearing is disabled';
      const selected = Array.isArray(data.value) ? data.value : data.value === null ? [] : [data.value];
      if (!unique(selected) || (multiple && p.maxCount !== undefined && selected.length > (p.maxCount as number))) {
        return 'invalid selected count';
      }
      const initial = p.value !== undefined ? p.value : p.defaultValue;
      const retained = Array.isArray(initial) ? initial : initial === undefined || initial === null ? [] : [initial];
      const options = p.options as { value: string | number; disabled?: boolean }[];
      return selected.every((value) => {
        const option = options.find((item) => item.value === value);
        if (option?.disabled) return retained.includes(value);
        return p.mode === 'tags' ? typeof value === 'string' && nonEmpty(value) : !!option;
      }) ? null : 'selected value is not allowed';
    }
    if (p.mode === 'tags') return typeof data.value === 'string' && nonEmpty(data.value) ? null : 'invalid tag';
    return (p.options as { value: string | number; disabled?: boolean }[])
      .some((item) => item.value === data.value && !item.disabled) ? null : 'selected value is not allowed';
  },
});
