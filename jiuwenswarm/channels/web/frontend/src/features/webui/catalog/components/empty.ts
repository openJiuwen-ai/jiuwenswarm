import { component, text, validURL } from './shared.ts';
export const emptyRule = component('Empty', { image: text, description: text }, {
  validate: (p) => p.image !== undefined && !['default', 'simple'].includes(p.image as string) && !validURL(p.image)
    ? 'invalid Empty image' : null,
});
