import { bool, checkURLs, compactSize, component, text, token } from './shared.ts';
export const cardRule = component('Card', {
  title: text, extra: text, cover: text, hoverable: bool, size: compactSize, variant: token('outlined', 'borderless'),
}, {
  allowChildren: true, defaults: { size: 'medium', variant: 'outlined', hoverable: false },
  validate: (p) => checkURLs(p, ['cover']),
});
