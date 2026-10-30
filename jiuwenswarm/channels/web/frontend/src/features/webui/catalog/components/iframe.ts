import { component, text, validURL } from './shared.ts';

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

export const iframeRule = component('Iframe', { src: text, title: { kind: 'string', maxBytes: 200 } }, {
  required: ['src', 'title'],
  validate(props) {
    if (typeof props.title !== 'string' || props.title.trim().length === 0) return 'Iframe title is required';
    return validIframeURL(props.src) ? null : 'invalid Iframe URL';
  },
});
