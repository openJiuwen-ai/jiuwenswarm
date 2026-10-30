import { checkURLs, component, text, token, union } from './shared.ts';

export const avatarRule = component('Avatar', {
  url: text,
  alt: text,
  size: union(token('small', 'default', 'large'), { kind: 'number', min: 1, max: 4096 }),
  shape: token('circle', 'square'),
}, {
  required: ['url'],
  defaults: { size: 'default', shape: 'circle' },
  validate: (p) => checkURLs(p, ['url']),
});
