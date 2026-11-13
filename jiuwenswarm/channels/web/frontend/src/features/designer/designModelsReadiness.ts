// Design gate (frontend-only): a design prompt needs a chat model plus at
// least one video model and one image model in the multimodal catalogue.
// The agent tool switch is ignored. Called from the design-mode entry points,
// the canvas chat send and single-node regenerate — other modes are untouched.

import i18n from '../../i18n';
import { webRequest } from '../../services/webClient';
import { useSessionStore } from '../../stores/sessionStore';
import { designChatModelReady } from './designChatModelState';
import { generationCatalogProblem, type GenerationKind } from './designGenerationReadiness';
import { nodeGenerationKind, type MaybeNode } from './designerNodeGenerationKind';
import type { DesignerExecutionGraph } from './executionGraphTypes';
import type { PublicMultimodalModel } from '../settings/services/multimodalSettings';
import type { ModelEntry } from '../../types';

/**
 * The design chat model problem, or null when a chat model is available.
 *
 * Design has no model switch: every leader round runs on the chat model from
 * Settings > Agent, so a missing one blocks both the landing prompt and the
 * canvas chat dock. The startup models fetch can time out while the runtime is
 * still warming up — re-verify with a fresh `models.list` before claiming the
 * chat model is missing, and fail open when the re-check cannot complete.
 */
export async function findDesignChatModelProblem(): Promise<string | null> {
  const loaded = useSessionStore.getState().chatAvailableModels;
  if (designChatModelReady(loaded)) {
    return null;
  }
  try {
    const resp = await webRequest<{ models?: ModelEntry[]; active_model?: string }>('models.list');
    const models = Array.isArray(resp?.models) ? resp.models : [];
    if (designChatModelReady(models)) {
      useSessionStore.getState().setAvailableModels(models, resp?.active_model ?? '');
      return null;
    }
  } catch {
    // Cannot verify right now: do not claim the chat model is missing.
    return null;
  }
  return i18n.t('designer.models.chatMissing');
}

async function loadGenerationModels(): Promise<PublicMultimodalModel[] | null> {
  try {
    const snapshot = await webRequest<{ models?: PublicMultimodalModel[] }>(
      'multimodal.models.list',
      {},
      { timeoutMs: 30_000 },
    );
    return Array.isArray(snapshot?.models) ? snapshot.models : [];
  } catch {
    return null;
  }
}

function problemsForKinds(models: readonly PublicMultimodalModel[], kinds: Iterable<GenerationKind>): string[] {
  const problems: string[] = [];
  for (const kind of kinds) {
    const problem = generationCatalogProblem(models, kind);
    if (problem) problems.push(problem);
  }
  return problems;
}

/**
 * Problems blocking a run of `nodes` (a whole workflow, or one node), or null.
 * Only the media kinds those nodes actually generate with are checked, so a
 * text-only graph is not blocked by an image model it would never call, and the
 * chat model keeps its existing handling (see `findDesignModelsProblem`).
 */
export async function findDesignRunProblem(nodes: readonly MaybeNode[], graph?: DesignerExecutionGraph | null): Promise<string | null> {
  const kinds = new Set<GenerationKind>();
  for (const node of nodes) {
    const kind = nodeGenerationKind(node, graph);
    if (kind) kinds.add(kind);
  }
  if (kinds.size === 0) return null;
  const models = await loadGenerationModels();
  if (!models) return null;
  const problems = problemsForKinds(models, kinds);
  return problems.length > 0 ? problems.join(' ') : null;
}

/**
 * User-facing problems blocking a design prompt, or null when a chat model
 * and at least one video model and one image model are available.
 * Never throws: a failed catalogue read must not block entry.
 */
export async function findDesignModelsProblem(): Promise<string | null> {
  const problems: string[] = [];
  // Chat readiness needs no catalogue read, so it must not be skipped when
  // the multimodal list times out while the runtime is warming up.
  const chatProblem = await findDesignChatModelProblem();
  if (chatProblem) problems.push(chatProblem);
  const models = await loadGenerationModels();
  if (!models) return problems.length > 0 ? problems.join(' ') : null;
  problems.push(...problemsForKinds(models, ['video', 'image']));
  return problems.length > 0 ? problems.join(' ') : null;
}
