import { array, bool, component, nonEmpty, object, text, token, union, unique } from './shared.ts';
import { WEBUI_ICON_NAMES } from '../../icons/names.ts';
import type { WebUIWireNode } from '../../contracts/wire.ts';

export type WebUIDropdownItem =
  | { type?: 'item'; key: string; label: string | WebUIWireNode[]; disabled?: boolean; danger?: boolean }
  | { type: 'divider'; key?: string };

export const dropdownRule = component('Dropdown', {
  items: array(union(
    object({ type: token('item'), key: text, label: union(text, { kind: 'nodes' }), disabled: bool, danger: bool }, ['key', 'label']),
    object({ type: token('divider'), key: text }, ['type']),
  )),
  placement: { kind: 'token', values: ['top', 'topLeft', 'topRight', 'bottom', 'bottomLeft', 'bottomRight',
    'left', 'leftTop', 'leftBottom', 'right', 'rightTop', 'rightBottom'] },
  align: object({
    points: array({ kind: 'token', values: ['tl', 'tc', 'tr', 'cl', 'cc', 'cr', 'bl', 'bc', 'br'] }),
    offset: array({ kind: 'number', min: -128, max: 128 }),
  }, ['points']),
  trigger: token('click', 'hover'),
  disabled: bool,
  ariaLabel: text,
  icon: token(...WEBUI_ICON_NAMES),
  autoFocus: bool,
  destroyOnHidden: bool,
  styles: object({ root: object({ width: { kind: 'number', min: 0, max: 4096 } }) }),
}, {
  required: ['items'], allowChildren: true,
  content: { prop: 'items', children: 'label' },
  events: { 'ui.select': object({ key: text }, ['key']) },
  defaults: { trigger: 'click', placement: 'topLeft', disabled: false },
  validate: (props) => {
    if (props.icon && !String(props.ariaLabel ?? '').trim()) return 'icon dropdowns require a non-empty ariaLabel';
    const align = props.align as { points: string[]; offset?: number[] } | undefined;
    if (align && (align.points.length !== 2 || (align.offset !== undefined && align.offset.length !== 2))) {
      return 'alignment requires two anchor points and two optional offsets';
    }
    const items = props.items as WebUIDropdownItem[];
    const keys = items.flatMap((item) => item.key === undefined ? [] : [item.key]);
    return items.length > 0 && keys.every(nonEmpty) && unique(keys)
      && items.every((item) => item.type === 'divider' || (nonEmpty(item.key)
        && (Array.isArray(item.label) ? item.label.length > 0 : nonEmpty(item.label))))
      ? null : 'menu items must have unique non-empty keys and labels';
  },
  validateEvent: (_event, data, props) => (props.items as WebUIDropdownItem[]).some(
    (item) => item.type !== 'divider' && !item.disabled && item.key === data.key,
  ) ? null : 'unknown or noninteractive menu key',
});
