import { bool, component, size, text, token } from './shared.ts';
export const dividerRule = component('Divider', {
  children: text, orientation: token('horizontal', 'vertical'), variant: token('solid', 'dashed', 'dotted'),
  titlePlacement: token('start', 'center', 'end'), plain: bool, size,
}, {
  defaults: { orientation: 'horizontal', variant: 'solid', titlePlacement: 'center', plain: false },
  allowChildren: true, childComponents: ['Text'],
  validate: (p) => p.orientation === 'vertical' && p.children ? 'vertical Divider cannot have a title' : null,
});
