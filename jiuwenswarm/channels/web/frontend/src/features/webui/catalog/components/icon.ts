import { component, size, text, token } from './shared.ts';
import { WEBUI_ICON_NAMES } from '../../icons/names.ts';

/** Leaf icon: artwork comes from the shared whitelist; a label is optional but must stay meaningful. */
export const iconRule = component('Icon', {
  name: token(...WEBUI_ICON_NAMES),
  size,
  'aria-label': text,
}, {
  required: ['name'],
  validate: (props) => {
    const label = props['aria-label'];
    return label !== undefined && !String(label).trim() ? 'icon label must be non-empty' : null;
  },
});
