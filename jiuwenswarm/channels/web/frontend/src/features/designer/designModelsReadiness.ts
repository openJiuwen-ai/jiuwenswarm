// Design landing gate (frontend-only): before the first prompt creates a
// session / graph, both generation slots (video / image) and the chat model
// must be configured and switched on in Settings. Reuses the Settings
// contract (`config.get` + generation-slot helpers) so the check reports
// exactly what the Settings page shows. Called from the design-mode entry
// points only — other modes are untouched.

import i18n from '../../i18n';
import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { designChatModelReady, designChatModelUsesAnthropic } from './designChatModelState';
import { generationProblemFromConfig, type GenerationKind } from './designGenerationReadiness';
import { nodeGenerationKind, type MaybeNode } from './designerNodeGenerationKind';
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
function selectedDesignChatModelName(sessionId?: string | null): string | null {
  const id = String(sessionId || useChatStore.getState().activeSessionId || '').trim();
  if (!id) return null;
  return useSessionStore.getState().runtimes[id]?.selectedModelName ?? null;
}

/**
 * Anthropic-protocol block for the chat model already in memory, or null.
 *
 * Synchronous on purpose: send / regenerate clicks must refuse before any
 * design RPC. A missing catalog is not this error — the async readiness
 * check still owns "chat model not configured".
 */
export function loadedDesignChatAnthropicProblem(
  sessionId?: string | null,
  selectedModelName?: string | null,
): string | null {
  const session = useSessionStore.getState();
  const selected = selectedModelName ?? selectedDesignChatModelName(sessionId);
  if (!designChatModelUsesAnthropic(session.chatAvailableModels, selected, session.defaultModelName)) {
    return null;
  }
  return i18n.t('designer.models.chatAnthropicUnsupported');
}

export async function findDesignChatModelProblem(sessionId?: string | null): Promise<string | null> {
  const loaded = useSessionStore.getState().chatAvailableModels;
  if (designChatModelReady(loaded)) {
    return loadedDesignChatAnthropicProblem(sessionId);
  }
  try {
    const resp = await webRequest<{ models?: ModelEntry[]; active_model?: string }>('models.list');
    const models = Array.isArray(resp?.models) ? resp.models : [];
    if (designChatModelReady(models)) {
      useSessionStore.getState().setAvailableModels(models, resp?.active_model ?? '');
      return loadedDesignChatAnthropicProblem(sessionId);
    }
  } catch {
    // Cannot verify right now: do not claim the chat model is missing.
    return null;
  }
  return i18n.t('designer.models.chatMissing');
}

/**
 * Problems blocking a run of `nodes` (a whole workflow, or one node), or null.
 * Only the media kinds those nodes actually generate with are checked, so a
 * text-only graph is not blocked by an image slot it would never call, and the
 * chat model keeps its existing handling (see `findDesignModelsProblem`).
 */
export async function findDesignRunProblem(nodes: readonly MaybeNode[]): Promise<string | null> {
  const kinds = new Set<GenerationKind>();
  for (const node of nodes) {
    const kind = nodeGenerationKind(node);
    if (kind) kinds.add(kind);
  }
  if (kinds.size === 0) return null;
  let config: Record<string, unknown> | null = null;
  try {
    config = (await webRequest<Record<string, unknown>>('config.get')) ?? {};
  } catch {
    config = null;
  }
  if (!config) return null;
  const problems: string[] = [];
  for (const kind of kinds) {
    const problem = generationProblemFromConfig(config, kind);
    if (problem) problems.push(problem);
  }
  return problems.length > 0 ? problems.join(' ') : null;
}

/**
 * User-facing problems blocking a design prompt, or null when everything the
 * design pipeline drives (chat / video / image) is configured and switched on.
 * Never throws: a failed config read must not block entry.
 */
export async function findDesignModelsProblem(sessionId?: string | null): Promise<string | null> {
  const problems: string[] = [];
  // Chat readiness needs no config read, so it must not be skipped when
  // `config.get` times out while the runtime is warming up.
  const chatProblem = await findDesignChatModelProblem(sessionId);
  if (chatProblem) problems.push(chatProblem);
  let config: Record<string, unknown> | null = null;
  try {
    config = (await webRequest<Record<string, unknown>>('config.get')) ?? {};
  } catch {
    config = null;
  }
  if (!config) return problems.length > 0 ? problems.join(' ') : null;
  for (const kind of ['video', 'image'] as const) {
    const problem = generationProblemFromConfig(config, kind);
    if (problem) problems.push(problem);
  }
  return problems.length > 0 ? problems.join(' ') : null;
}
