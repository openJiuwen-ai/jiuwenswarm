import type { DocSelection } from '../components/ArtifactsPanel/docSelection';
import { encodeArtifactSelectionMessage } from '../components/ArtifactsPanel/artifactSelectionMessage';
import {
  readArtifactAiEditSubmitMode,
  type ArtifactAiEditSubmitMode,
} from './artifactAiEditPreference';

export type PreviewAiEditRequest = {
  id: string;
  /** Message content stored in the user bubble (selection marker + instruction). */
  displayContent: string;
  /** Instruction only — used for fill_only composer draft. */
  instruction: string;
  selection: DocSelection;
  mode: ArtifactAiEditSubmitMode;
};

let current: PreviewAiEditRequest | null = null;
/** Held across fill_only so submit can re-wrap the edited instruction. */
let pendingSelection: DocSelection | null = null;
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of listeners) listener();
}

export function submitPreviewAiEdit(selection: DocSelection, instruction: string): void {
  const body = (instruction || '').trim();
  current = {
    id: `preview-ai-edit-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    displayContent: encodeArtifactSelectionMessage(selection, body),
    instruction: body,
    selection,
    mode: readArtifactAiEditSubmitMode(),
  };
  pendingSelection = selection;
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

export function takePendingArtifactSelection(): DocSelection | null {
  const sel = pendingSelection;
  pendingSelection = null;
  return sel;
}

export function peekPendingArtifactSelection(): DocSelection | null {
  return pendingSelection;
}

export function clearPendingArtifactSelection(): void {
  pendingSelection = null;
}

export function subscribePreviewAiEdit(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}
