import { bool, component, text, token } from './shared.ts';

const MEDIA_PREFIX = '/extension-assets/';
const PATH_CHARS = /^[A-Za-z0-9\-._~!$&'()*+,;=:@%/]*$/;
// Printable ASCII without the space and the backslash.
const FORBIDDEN = /[^\x21-\x5b\x5d-\x7e]/;
const BAD_ESCAPE = /%(?![0-9A-Fa-f]{2})/;
const BAD_SEGMENT = /[/\\\u0000-\u001f\u007f]/;

/** Site-relative package or data asset paths; the Gateway valid_media_url applies the same steps. */
export function validMediaURL(value: unknown): boolean {
  if (typeof value !== 'string' || FORBIDDEN.test(value)) return false;
  const path = value.split(/[?#]/, 1)[0];
  if (!path.startsWith(MEDIA_PREFIX) || !PATH_CHARS.test(path) || BAD_ESCAPE.test(path)) return false;
  const segments = path.slice(MEDIA_PREFIX.length).split('/');
  return segments.length >= 3 && segments.every((segment) => {
    if (!segment) return false;
    let decoded: string;
    try {
      decoded = decodeURIComponent(segment);
    } catch {
      return false;
    }
    return decoded !== '.' && decoded !== '..' && !BAD_SEGMENT.test(decoded);
  });
}

const range = (min: number, max: number) => ({ kind: 'number', min, max }) as const;

export const mediaSurfaceRule = component('MediaSurface', {
  kind: token('image', 'video', 'html'), src: text, title: { kind: 'string', maxBytes: 200 }, poster: text,
  fit: token('cover', 'contain'), position: token('center', 'top', 'bottom', 'left', 'right'),
  loop: bool, muted: bool, dim: range(0, 0.9), blur: range(0, 40), opacity: range(0.1, 1),
}, {
  required: ['kind', 'src', 'title'],
  defaults: { fit: 'cover', position: 'center', loop: true, muted: true, dim: 0, blur: 0, opacity: 1 },
  validate(props) {
    if (typeof props.title !== 'string' || props.title.trim().length === 0) return 'MediaSurface title is required';
    return [props.src, props.poster].every((value) => value === undefined || validMediaURL(value))
      ? null : 'invalid MediaSurface URL';
  },
});
