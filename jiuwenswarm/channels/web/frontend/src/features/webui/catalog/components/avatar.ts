import { checkURLs, component, object, text, token, union } from './shared.ts';

export const avatarRule = component('Avatar', {
  src: text,
  children: text,
  alt: text,
  gap: { kind: 'number', min: 0, max: 4096 },
  style: object({
    backgroundColor: { kind: 'string', maxBytes: 9 },
    color: { kind: 'string', maxBytes: 9 },
    fontSize: { kind: 'number', min: 1, max: 4096 },
  }),
  size: union(token('small', 'medium', 'large', 'sm', 'md', 'lg', 'default'), { kind: 'number', min: 1, max: 4096 }),
  shape: token('circle', 'square'),
}, {
  defaults: { size: 'medium', shape: 'circle' },
  validate: (p) => {
    const style = (p.style ?? {}) as Record<string, unknown>;
    const invalidColor = ['backgroundColor', 'color'].some((key) => style[key] !== undefined
      && !/^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(style[key] as string));
    return checkURLs(p, ['src']) ?? (invalidColor ? 'Avatar colors must use hexadecimal notation' : null);
  },
});
