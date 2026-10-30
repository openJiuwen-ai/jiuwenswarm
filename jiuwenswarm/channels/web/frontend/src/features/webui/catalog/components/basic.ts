import type { WebUIComponentRule, WebUIPropRule } from '../types.ts';
import { noData } from './shared.ts';

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
      gap: token(['none', 'sm', 'md', 'lg']),
      align: token(['stretch', 'start', 'center', 'end', 'baseline']),
      justify: token(['start', 'center', 'end', 'between', 'around', 'evenly']),
      wrap: { kind: 'boolean' },
      // 对应 CSS flex 简写（flex: 1 / flex: 2 ...），即 flex-grow 系数。
      flex: { kind: 'number', min: 0, max: 1000 },
    },
    defaults: {
      direction: 'row',
      gap: 'md',
      align: 'stretch',
      justify: 'start',
      wrap: false,
    },
    allowChildren: true,
  },
  {
    name: 'Text',
    props: {
      value: { kind: 'string', maxBytes: 16 * 1024 },
      variant: token(['default', 'primary', 'secondary', 'danger', 'ghost']),
      tone: token(['neutral', 'info', 'success', 'warning', 'danger']),
      size: token(['sm', 'md', 'lg']),
      weight: token(['regular', 'medium', 'bold']),
      max_lines: { kind: 'number' },
      monospace: { kind: 'boolean' },
    },
    allowChildren: false,
  },
  {
    name: 'Button',
    props: {
      label: { kind: 'string', maxBytes: 16 * 1024 },
      variant: token(['default', 'primary', 'secondary', 'danger', 'ghost']),
      size: token(['sm', 'md', 'lg']),
      disabled: { kind: 'boolean' },
    },
    allowChildren: false,
    events: { 'ui.click': noData },
  },
];
