import {
  array, bool, component, inputProps, integer, nil, noData, nonEmpty, number, object,
  text, token, union, unique, valueData,
} from './shared.ts';

const optionValue = union(text, number);
export const selectRule = component('Select', {
  ...inputProps,
  options: array(object({ value: optionValue, label: text, disabled: bool }, ['value', 'label'])),
  value: union(optionValue, nil, array(optionValue)), defaultValue: union(optionValue, nil, array(optionValue)),
  placeholder: text, allowClear: bool, mode: token('multiple', 'tags'), maxCount: integer(1),
  maxTagCount: union(integer(), token('responsive')), showSearch: bool,
}, {
  required: ['options'], defaults: { size: 'medium', variant: 'outlined' },
  events: {
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
});
