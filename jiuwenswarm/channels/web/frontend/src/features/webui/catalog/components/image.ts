import { bool, checkURLs, component, number, object, text, token, union, validDimension, validURL } from './shared.ts';
export const imageRule = component('Image', {
  src: text, alt: text, width: union(number, text), height: union(number, text), fallback: text,
  preview: union(bool, object({ src: text }, ['src'])),
  fit: token('contain', 'cover'),
}, {
  required: ['src'], defaults: { preview: true },
  validate(p) {
    const error = checkURLs(p, ['src', 'fallback']);
    if (error) return error;
    if (typeof p.preview === 'object' && !validURL((p.preview as { src: string }).src)) return 'invalid preview URL';
    return ['width', 'height'].some((field) => p[field] !== undefined && !validDimension(p[field])) ? 'invalid image dimension' : null;
  },
});
