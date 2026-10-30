import type { WebUIComponentRule, WebUIPropRule } from '../types.ts';

export const text: WebUIPropRule = { kind: 'string' };
export const bool: WebUIPropRule = { kind: 'boolean' };
export const number: WebUIPropRule = { kind: 'number' };
export const nil: WebUIPropRule = { kind: 'null' };
export const token = (...values: string[]): WebUIPropRule => ({ kind: 'token', values });
export const array = (item: WebUIPropRule): WebUIPropRule => ({ kind: 'array', item });
export const union = (...choices: WebUIPropRule[]): WebUIPropRule => ({ kind: 'union', choices });
export const object = (fields: Record<string, WebUIPropRule>, required: string[] = []): WebUIPropRule =>
  ({ kind: 'object', fields, required });
export const integer = (min = 0): WebUIPropRule => ({ kind: 'number', min, integer: true });
export const size = token('small', 'medium', 'large');
export const variant = token('outlined', 'borderless', 'filled', 'underlined');
export const status = token('error', 'warning');
export const inputProps = { disabled: bool, ariaLabel: text, size, variant, status };
export const valueData = (value: WebUIPropRule): WebUIPropRule => object({ value }, ['value']);
export const noData = object({});

export function component(
  name: string,
  props: Record<string, WebUIPropRule>,
  options: Partial<Omit<WebUIComponentRule, 'name' | 'props'>> = {},
): WebUIComponentRule {
  return { name, props, allowChildren: false, ...options };
}

export function unique(values: unknown[]): boolean {
  return new Set(values).size === values.length;
}

export function nonEmpty(value: unknown): boolean {
  return typeof value === 'string' ? value.length > 0 : typeof value === 'number' && Number.isFinite(value);
}

/** Relative resources resolve against the host; URL parsing also catches disguised protocols. */
export function validURL(value: unknown): boolean {
  if (typeof value !== 'string' || value.trim().length === 0) return false;
  try {
    const url = new URL(value, 'https://webui.invalid/');
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch {
    return false;
  }
}

export function checkURLs(props: Readonly<Record<string, unknown>>, fields: string[]): string | null {
  return fields.some((field) => props[field] !== undefined && !validURL(props[field])) ? 'invalid resource URL' : null;
}

export const MAX_IMAGE_DIMENSION = 4096;
export function validDimension(value: unknown): boolean {
  if (typeof value === 'number') return value >= 0 && value <= MAX_IMAGE_DIMENSION;
  if (value === 'auto') return true;
  if (typeof value !== 'string') return false;
  const match = /^(\d+(?:\.\d+)?)(px|%)$/.exec(value);
  return !!match && Number(match[1]) <= (match[2] === '%' ? 100 : MAX_IMAGE_DIMENSION);
}
