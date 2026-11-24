import type { WebUIPropRule } from '../types.ts';
import { component, text } from './shared.ts';
import { hasNonEmptyLocalizedText, localizedTextRule } from './localizedText.ts';
import { validMediaURL } from './mediaSurface.ts';

const jsonValue: WebUIPropRule = {
  kind: 'lazy',
  get: () => ({ kind: 'union', choices: [
    text, { kind: 'boolean' }, { kind: 'number' }, { kind: 'null' },
    { kind: 'array', item: jsonValue }, { kind: 'record', value: jsonValue },
  ] }),
};

export const applicationSurfaceRule = component('ApplicationSurface', {
  src: text,
  title: localizedTextRule,
  width: { kind: 'number', integer: true, min: 160, max: 960 },
  height: { kind: 'number', integer: true, min: 120, max: 960 },
  config: { kind: 'record', value: jsonValue },
}, {
  required: ['src', 'title'],
  defaults: { width: 360, height: 440, config: {} },
  validate(props) {
    if (!hasNonEmptyLocalizedText(props.title)) return 'ApplicationSurface title is required';
    const titles = typeof props.title === 'string' ? [props.title] : Object.values(props.title as Record<string, string>);
    if (titles.some((title) => new TextEncoder().encode(title).byteLength > 200)) return 'ApplicationSurface title is too long';
    if (!validMediaURL(props.src)) return 'invalid ApplicationSurface URL';
    return new TextEncoder().encode(JSON.stringify(props.config)).byteLength > 16 * 1024
      ? 'ApplicationSurface config exceeds 16 KiB' : null;
  },
});
