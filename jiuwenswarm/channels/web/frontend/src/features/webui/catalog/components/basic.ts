import type { WebUIComponentRule, WebUIPropRule } from '../types.ts';
import { integer, prepareSize, size, validDimension, validURL } from './shared.ts';
import { WEBUI_ICON_NAMES } from '../../icons/names.ts';

function token(values: readonly string[]): WebUIPropRule {
  return { kind: 'token', values };
}

/** Shared layout, text and button rules for the component protocol. */
export const basicComponentRules: readonly WebUIComponentRule[] = [
  {
    // 布局容器：取代旧 Row，direction 决定行/列，其余 props 全部是受控布局 token。
    name: 'Box',
    props: {
      direction: token(['row', 'column']),
      gap: { kind: 'union', choices: [token(['none', 'sm', 'md', 'lg']), { kind: 'number', min: 0 }] },
      align: token(['stretch', 'start', 'center', 'end', 'baseline']),
      justify: token(['start', 'center', 'end', 'between', 'around', 'evenly']),
      wrap: { kind: 'boolean' },
      // 对应 CSS flex 简写（flex: 1 / flex: 2 ...），即 flex-grow 系数。
      flex: { kind: 'number', min: 0, max: 1000 },
      width: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      height: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      minHeight: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      maxWidth: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      marginInline: { kind: 'union', choices: [{ kind: 'number' }, { kind: 'string' }] },
      padding: token(['none', 'sm', 'md', 'lg']),
      backgroundImage: { kind: 'string' },
      backgroundFit: token(['cover', 'contain']),
      visibleFrom: token(['sm', 'md', 'lg']),
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
  {
    name: 'Text',
    props: {
      value: { kind: 'string', maxBytes: 16 * 1024 },
      variant: token(['default', 'primary', 'secondary', 'danger', 'ghost']),
      tone: token(['neutral', 'info', 'success', 'warning', 'danger']),
      size,
      weight: token(['regular', 'medium', 'bold']),
      max_lines: integer(1),
      monospace: { kind: 'boolean' },
    },
    allowChildren: false,
    prepare: prepareSize,
  },
  {
    name: 'Button',
    props: {
      label: { kind: 'string', maxBytes: 16 * 1024 },
      variant: token(['default', 'primary', 'secondary', 'danger', 'ghost']),
      size,
      disabled: { kind: 'boolean' },
      icon: token(WEBUI_ICON_NAMES),
      iconOnly: { kind: 'boolean' },
      form: { kind: 'string', maxBytes: 256 },
    },
    allowChildren: false,
    events: { 'ui.click': { kind: 'record', value: { kind: 'string' } } },
    prepare: prepareSize,
    validate: (props) => props.iconOnly === true && (!props.icon || !String(props.label ?? '').trim())
      ? 'icon-only buttons require an icon and a non-empty label' : null,
    validateEvent: (_event, data, props) => props.form === undefined && Object.keys(data).length > 0
      ? 'ordinary Button does not accept form data' : null,
  },
];
