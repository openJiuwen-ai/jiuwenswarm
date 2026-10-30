import { component } from './shared.ts';

export const panelRule = component('Panel', { title: { kind: 'string', maxBytes: 200 } }, {
  required: ['title'],
  allowChildren: true,
  validate: (props) => typeof props.title === 'string' && props.title.trim().length > 0 ? null : 'Panel title is required',
});
