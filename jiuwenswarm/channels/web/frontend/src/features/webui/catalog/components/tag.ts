import { bool, checkURLs, component, text, token } from './shared.ts';
export const tagRule = component('Tag', {
  children: text, color: token('default', 'success', 'processing', 'warning', 'error'),
  variant: token('filled', 'solid', 'outlined'), disabled: bool, href: text, target: token('_self', '_blank'),
}, {
  defaults: { variant: 'filled', disabled: false, target: '_self' }, validate: (p) => checkURLs(p, ['href']),
});
