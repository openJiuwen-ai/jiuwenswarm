import type { WebUIPropRule } from '../types.ts';
import {
  array, bool, component, inputProps, integer, nil, noData, object, text, token, union, unique, valueData,
} from './shared.ts';

const treeOption: WebUIPropRule = object({
  key: text, value: text, title: text, disabled: bool, disableCheckbox: bool, selectable: bool, checkable: bool,
  children: array({ kind: 'lazy', get: () => treeOption }),
}, ['value', 'title']);
const selection = union(text, nil, array(text));
export const treeSelectRule = component('TreeSelect', {
  ...inputProps, treeData: array(treeOption), value: selection, defaultValue: selection,
  placeholder: text, allowClear: bool, multiple: bool, treeCheckable: bool,
  showCheckedStrategy: token('SHOW_CHILD', 'SHOW_PARENT', 'SHOW_ALL'), showSearch: bool,
  treeDefaultExpandAll: bool, treeDefaultExpandedKeys: array(text), maxTagCount: integer(),
}, {
  required: ['treeData'], defaults: { size: 'medium', variant: 'outlined', showCheckedStrategy: 'SHOW_CHILD' },
  events: { 'ui.select': valueData(text), 'ui.change': valueData(selection), 'ui.clear': noData },
  validate(p) {
    const values: string[] = [];
    const keys: string[] = [];
    let invalid = false;
    function walk(items: Record<string, unknown>[], depth: number): void {
      if (!items.length) return;
      if (depth > 8) { invalid = true; return; }
      for (const item of items) {
        const value = item.value as string;
        const key = (item.key ?? value) as string;
        if (!value || !key) invalid = true;
        values.push(value); keys.push(key);
        if (values.length > 512) { invalid = true; return; }
        walk((item.children ?? []) as Record<string, unknown>[], depth + 1);
      }
    }
    walk(p.treeData as Record<string, unknown>[], 1);
    if (invalid || !unique(values) || !unique(keys)) return 'invalid tree option identities or budget';
    if (p.treeCheckable && p.multiple === false) return 'treeCheckable conflicts with multiple=false';
    const multiple = p.treeCheckable === true || p.multiple === true;
    for (const field of ['value', 'defaultValue']) {
      const value = p[field];
      if (value === undefined) continue;
      if (multiple !== Array.isArray(value)) return 'selection does not match tree mode';
      const list = Array.isArray(value) ? value : value === null ? [] : [value];
      if (!unique(list) || list.some((item) => !values.includes(item))) return 'unknown tree selection';
    }
    const expanded = (p.treeDefaultExpandedKeys ?? []) as string[];
    if (!unique(expanded) || expanded.some((key) => !keys.includes(key))) return 'unknown expanded key';
    return p.treeDefaultExpandAll && expanded.length ? 'conflicting default expansion sources' : null;
  },
  prepare: (p) => ({ ...p, multiple: p.treeCheckable === true || p.multiple === true,
    showSearch: p.showSearch ?? (p.treeCheckable === true || p.multiple === true) }),
  validateEvent(event, data, p) {
    if (event === 'ui.clear') return p.allowClear ? null : 'clearing is disabled';
    const values = new Set<string>();
    function walk(items: Record<string, unknown>[]): void {
      for (const item of items) {
        const enabled = p.treeCheckable
          ? item.checkable !== false && !item.disableCheckbox : item.selectable !== false;
        if (!item.disabled && enabled) values.add(item.value as string);
        walk((item.children ?? []) as Record<string, unknown>[]);
      }
    }
    walk(p.treeData as Record<string, unknown>[]);
    const multiple = event === 'ui.change' && (p.treeCheckable === true || p.multiple === true);
    if (multiple !== Array.isArray(data.value)) return 'selection does not match tree mode';
    const selected = Array.isArray(data.value) ? data.value : data.value === null ? [] : [data.value];
    return unique(selected) && selected.every((value) => values.has(value as string))
      ? null : 'selected value is not allowed';
  },
});
