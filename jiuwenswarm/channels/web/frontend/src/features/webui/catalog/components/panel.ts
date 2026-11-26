import { component, token } from './shared.ts';
import { WEBUI_ICON_NAMES } from '../../icons/names.ts';

export const panelRule = component('Panel', {
  title: { kind: 'string', maxBytes: 200 },
  icon: token(...WEBUI_ICON_NAMES),
}, {
  required: ['title'],
  allowChildren: true,
  validate: (props) => typeof props.title === 'string' && props.title.trim().length > 0 ? null : 'Panel title is required',
});
