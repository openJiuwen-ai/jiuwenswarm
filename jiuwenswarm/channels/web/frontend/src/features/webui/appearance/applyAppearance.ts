import type { WebUIAppearance } from './appearance.ts';
import { buildAppearanceCss, readSurfaceBases } from './derive.ts';

export const APPEARANCE_STYLE_ID = 'webui-appearance';
let writes = 0;

/** Write or remove the appearance contribution layer of a document. */
export function applyAppearance(doc: Document, appearance: WebUIAppearance | null): void {
  const root = doc.documentElement;
  const existing = doc.getElementById(APPEARANCE_STYLE_ID);
  if (!appearance) {
    existing?.remove();
    root.removeAttribute('data-webui-appearance');
    root.removeAttribute('data-webui-surface');
    return;
  }
  const style = existing && existing.tagName === 'STYLE' ? existing : doc.createElement('style');
  style.id = APPEARANCE_STYLE_ID;
  // Read the theme's own surface colors with the previous contribution removed.
  style.textContent = '';
  const computed = doc.defaultView?.getComputedStyle(root);
  const bases = readSurfaceBases((name) => computed?.getPropertyValue(name) ?? '');
  style.textContent = buildAppearanceCss(appearance, bases);
  if (!style.isConnected) doc.head.append(style);
  // A new value on every write lets theme observers re-read the variables after the style changes.
  writes += 1;
  root.setAttribute('data-webui-appearance', String(writes));
  root.setAttribute('data-webui-surface', appearance.surface ?? 'opaque');
}
