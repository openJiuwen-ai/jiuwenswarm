import { component, validURL } from './shared.ts';
import { hasNonEmptyLocalizedText, localizedTextRule } from './localizedText.ts';

function validIframeURL(value: unknown): boolean {
  if (typeof value !== 'string' || !validURL(value) || /[\u0000-\u001f\u007f\\]/.test(value)) return false;
  const source = value.trim();
  // Keep browser and backend URL parsing aligned for absolute and relative sources.
  if (source.startsWith('///') || (/^[a-z][a-z0-9+.-]*:/i.test(source) && !/^https?:\/\//i.test(source))) {
    return false;
  }
  const authority = /^(?:https?:)?\/\/([^/?#]*)/i.exec(source)?.[1];
  if (authority !== undefined && (!authority || /[\s%@<>^|]/.test(authority))) return false;
  try {
    const url = new URL(source, 'https://webui.invalid/');
    return !url.username && !url.password;
  } catch {
    return false;
  }
}

export const iframeRule = component(
  'Iframe',
  {
    src: localizedTextRule,
    srcDoc: localizedTextRule,
    title: localizedTextRule,
    minWidth: { kind: 'number', min: 0, max: 4096 },
  },
  {
    required: ['title'],
    validate(props) {
      if (!hasNonEmptyLocalizedText(props.title)) return 'Iframe title is required';
      const titles =
        typeof props.title === 'string' ? [props.title] : Object.values(props.title as Record<string, string>);
      if (titles.some((title) => new TextEncoder().encode(title).byteLength > 200)) return 'Iframe title is too long';
      if ((props.src !== undefined) === (props.srcDoc !== undefined))
        return 'Iframe requires exactly one document source';
      if (props.srcDoc !== undefined)
        return hasNonEmptyLocalizedText(props.srcDoc) ? null : 'Iframe document is required';
      const sources = typeof props.src === 'string' ? [props.src] : Object.values(props.src as Record<string, string>);
      return sources.every(validIframeURL) ? null : 'invalid Iframe URL';
    },
  },
);
