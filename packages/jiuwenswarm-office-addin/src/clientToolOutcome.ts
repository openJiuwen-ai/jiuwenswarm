import type { ClientToolResponse, MutationRecord } from './clientToolDispatcher';

/** Convert what a write reports about itself into the one mutation contract seen by the agent. */
export function withMutationRecord(response: ClientToolResponse): ClientToolResponse {
  if (response.mutation) return response;
  const evidence = response.mutationEvidence ?? {};
  const error = response.ok ? undefined : response.error;
  const details = error?.details ?? {};
  const rawOutcome = evidence.outcome ?? details.outcome;
  const state = normalizeMutationState(rawOutcome, response.ok);
  const replacementSlideIds = slideIdReplacements(evidence.slide_id_updates);
  // A failed call may carry its evidence in `error.details` only: whatever
  // slides it resolved or created before the failure. Fall back to it the same
  // way `rawOutcome` does, so a partial/uncertain native write is not reported
  // as touching nothing.
  const createdSlideIds = strings(evidence.created_slide_ids ?? details.created_slide_ids);
  const deletedSlideIds = strings(evidence.deleted_slide_ids ?? details.deleted_slide_ids);
  const affectedSlideIds = strings(evidence.affected_slide_ids ?? details.affected_slide_ids);
  // An executor that names its verification targets (the scene patch leaves
  // out pure reorders and deletions) is authoritative; otherwise every
  // touched slide needs a fresh look.
  const verificationTargets = Array.isArray(evidence.verification_targets)
    ? unique([...strings(evidence.verification_targets), ...Object.values(replacementSlideIds)])
    : unique([...createdSlideIds, ...affectedSlideIds, ...Object.values(replacementSlideIds)]);
  const nativeMutationStarted = typeof evidence.native_mutation_started === 'boolean'
    ? evidence.native_mutation_started
    : typeof details.native_mutation_started === 'boolean'
      ? details.native_mutation_started
      : state === 'applied' || state === 'partial';
  const lastCompletedStage = stringValue(evidence.last_completed_stage)
    ?? stringValue(details.last_completed_stage);
  const resolvedSlides = resolvedSlideList(evidence.resolved_slides ?? details.resolved_slides);
  const mutation: MutationRecord = {
    state,
    native_mutation_started: nativeMutationStarted,
    ...(lastCompletedStage ? { last_completed_stage: lastCompletedStage } : {}),
    ...(createdSlideIds.length ? { created_slide_ids: createdSlideIds } : {}),
    ...(deletedSlideIds.length ? { deleted_slide_ids: deletedSlideIds } : {}),
    ...(affectedSlideIds.length ? { affected_slide_ids: affectedSlideIds } : {}),
    ...(Object.keys(replacementSlideIds).length ? { replacement_slide_ids: replacementSlideIds } : {}),
    ...(verificationTargets.length ? { verification_targets: verificationTargets } : {}),
    ...(resolvedSlides.length ? { resolved_slides: resolvedSlides } : {}),
    safe_to_retry: state === 'not_applied' && (response.ok || error?.retryable === true),
  };
  return { ...response, mutation };
}

function normalizeMutationState(value: unknown, succeeded: boolean): MutationRecord['state'] {
  if (value === 'not_applied') return 'not_applied';
  if (value === 'applied') return 'applied';
  if (value === 'partial' || value === 'partially_applied') return 'partial';
  if (value === 'uncertain' || value === 'unknown' || value === 'pending') return 'uncertain';
  return succeeded ? 'uncertain' : 'not_applied';
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? unique(value.filter((item): item is string => typeof item === 'string' && item.length > 0))
    : [];
}

function slideIdReplacements(value: unknown): Record<string, string> {
  if (!Array.isArray(value)) return {};
  return Object.fromEntries(value.flatMap(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
    const update = item as Record<string, unknown>;
    const oldId = stringValue(update.old_slide_id) ?? stringValue(update.before);
    const newId = stringValue(update.new_slide_id) ?? stringValue(update.after);
    return oldId && newId ? [[oldId, newId]] : [];
  }));
}

function resolvedSlideList(
  value: unknown,
): { slide_ref: string; slide_id: string; created: boolean }[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
    const entry = item as Record<string, unknown>;
    const slideRef = stringValue(entry.slide_ref);
    const slideId = stringValue(entry.slide_id);
    if (!slideRef || !slideId) return [];
    return [{ slide_ref: slideRef, slide_id: slideId, created: entry.created === true }];
  });
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function unique(values: string[]): string[] {
  return [...new Set(values)];
}
