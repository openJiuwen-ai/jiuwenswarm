import type { WebUIComponentRule } from '../types.ts';
import {
  bool, component, number, object, presetColors, size, text, token, union, validDimension, validURL,
} from './shared.ts';
import { WEBUI_ICON_NAMES } from '../../icons/names.ts';

/** Shared layout, text and button rules for the component protocol. */
export const basicComponentRules: readonly WebUIComponentRule[] = [
  {
    // 布局容器：取代旧 Row，direction 决定行/列，其余 props 全部是受控布局 token。
    name: 'Box',
    props: {
      direction: token('row', 'column'),
      gap: union(token('none', 'sm', 'md', 'lg'), { kind: 'number', min: 0 }),
      align: token('stretch', 'start', 'center', 'end', 'baseline'),
      justify: token('start', 'center', 'end', 'between', 'around', 'evenly'),
      wrap: bool,
      // 对应 CSS flex 简写（flex: 1 / flex: 2 ...），即 flex-grow 系数。
      flex: { kind: 'number', min: 0, max: 1000 },
      width: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      height: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      minHeight: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      maxWidth: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      marginInline: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      padding: token('none', 'sm', 'md', 'lg'),
      backgroundImage: { kind: 'string' },
      backgroundFit: token('cover', 'contain'),
      visibleFrom: token('sm', 'md', 'lg'),
    },
    defaults: {
      direction: 'row',
      gap: 'md',
      align: 'stretch',
      justify: 'start',
      wrap: false,
    },
    allowChildren: true,
    validate: (props) => {
      if (['width', 'height', 'minHeight', 'maxWidth'].some((field) => props[field] !== undefined
        && (!validDimension(props[field]) || (['minHeight', 'maxWidth'].includes(field) && props[field] === 'auto')))) {
        return 'invalid Box dimension';
      }
      if (props.marginInline !== undefined && !validDimension(props.marginInline)) return 'invalid Box inline margin';
      return props.backgroundImage !== undefined && !validURL(props.backgroundImage) ? 'invalid background URL' : null;
    },
  },
  component('Text', {
    value: text,
    type: token('secondary', 'success', 'warning', 'danger'),
    size,
    strong: bool,
    italic: bool,
    underline: bool,
    delete: bool,
    code: bool,
    mark: bool,
    keyboard: bool,
    disabled: bool,
    ellipsis: bool,
  }),
  component('Button', {
    // The host maps the protocol text value to Ant Design children.
    value: text,
    type: token('default', 'primary', 'dashed', 'link', 'text'),
    color: token('default', 'primary', 'danger', ...presetColors),
    variant: token('outlined', 'dashed', 'solid', 'filled', 'text', 'link'),
    shape: token('default', 'circle', 'round', 'square'),
    size,
    danger: bool,
    ghost: bool,
    disabled: bool,
    block: bool,
    loading: union(bool, object({ delay: number })),
    href: text,
    target: token('_self', '_blank'),
    icon: token(...WEBUI_ICON_NAMES),
    iconPlacement: token('start', 'end'),
    'aria-label': text,
    form: { kind: 'string', maxBytes: 200 },
  }, {
    events: { 'ui.click': { kind: 'record', value: text } },
    validate: (props) => {
      if (props.form !== undefined && !String(props.form).trim()) return 'form reference is required';
      const label = String(props.value ?? '').trim();
      const accessible = String(props['aria-label'] ?? '').trim();
      if (props.icon && !label && !accessible) return 'icon-only buttons require a non-empty aria-label';
      if (props.href !== undefined && !validURL(props.href)) return 'invalid button URL';
      return props.target !== undefined && props.href === undefined ? 'button target requires href' : null;
    },
    validateEvent: (_event, data, props) => props.form === undefined && Object.keys(data).length > 0
      ? 'ordinary Button does not accept form data' : null,
  }),
];
