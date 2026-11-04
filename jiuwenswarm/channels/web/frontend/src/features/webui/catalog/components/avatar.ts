import { checkURLs, component, text, token, union } from './shared.ts';

export const avatarRule = component('Avatar', {
  src: text,
  alt: text,
  size: union(token('small', 'medium', 'large', 'sm', 'md', 'lg', 'default'), { kind: 'number', min: 1, max: 4096 }),
  shape: token('circle', 'square'),
}, {
  required: ['src'],
  defaults: { size: 'medium', shape: 'circle' },
  validate: (p) => checkURLs(p, ['src']),
});
