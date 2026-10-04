import {
  readArtifactAiEditSubmitMode,
  type ArtifactAiEditSubmitMode,
} from './artifactAiEditPreference';

export type PreviewAiEditRequest = {
  id: string;
  prompt: string;
  mode: ArtifactAiEditSubmitMode;
};

let current: PreviewAiEditRequest | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function submitPreviewAiEdit(prompt: string): void {
  const text = (prompt || '').trim();
  if (!text) return;
  current = {
    id: `preview-ai-edit-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    prompt: text,
    mode: readArtifactAiEditSubmitMode(),
  };
  notify();
}

export function getPreviewAiEditRequest(): PreviewAiEditRequest | null {
  return current;
}

export function consumePreviewAiEditRequest(): PreviewAiEditRequest | null {
  const req = current;
  current = null;
  return req;
}

export function subscribePreviewAiEdit(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
