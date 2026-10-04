import type { DocSelection } from './docSelection';
import { composeAiEditPrompt } from './previewSelection';

export type ArtifactSelectionPayload = {
  kind: DocSelection['kind'];
  source: string;
  path: string;
  range: string;
  quote: string;
  /** User instruction stored in the marker so the bubble has no trailing blank lines. */
  instruction?: string;
};

const MARKER_RE = /\{\{artifact-selection:([A-Za-z0-9+/=]+)\}\}/g;

function utf8ToBase64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = '';
  bytes.forEach(byte => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary);
}

function base64ToUtf8(encoded: string): string {
  const binary = atob(encoded);
  const bytes = Uint8Array.from(binary, ch => ch.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

export function encodeArtifactSelectionMessage(sel: DocSelection, instruction: string): string {
  const body = (instruction || '').trim();
  const payload: ArtifactSelectionPayload = {
    kind: sel.kind,
    source: sel.source,
    path: sel.path,
    range: sel.range,
    quote: sel.preview,
    ...(body ? { instruction: body } : {}),
  };
  return `{{artifact-selection:${utf8ToBase64(JSON.stringify(payload))}}}`;
}

export function parseArtifactSelectionPayload(encoded: string): ArtifactSelectionPayload | null {
  try {
    const raw = JSON.parse(base64ToUtf8(encoded)) as Partial<ArtifactSelectionPayload>;
    if (!raw || typeof raw.quote !== 'string' || typeof raw.source !== 'string') return null;
    return {
      kind: (raw.kind as ArtifactSelectionPayload['kind']) || 'text',
      source: raw.source,
      path: typeof raw.path === 'string' ? raw.path : '',
      range: typeof raw.range === 'string' ? raw.range : '',
      quote: raw.quote,
      instruction: typeof raw.instruction === 'string' ? raw.instruction : undefined,
    };
  } catch {
    return null;
  }
}

/** Expand selection markers into model-facing @file / quote prompts. */
export function expandArtifactSelectionForModel(content: string): string {
  return content
    .replace(MARKER_RE, (_full, encoded: string) => {
      const payload = parseArtifactSelectionPayload(encoded);
      if (!payload) return '';
      return composeAiEditPrompt(
        {
          kind: payload.kind,
          source: payload.source,
          path: payload.path,
          range: payload.range,
          preview: payload.quote,
        },
        payload.instruction || '',
      );
    })
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

export function hasArtifactSelectionMarker(content: string): boolean {
  MARKER_RE.lastIndex = 0;
  return MARKER_RE.test(content);
}
