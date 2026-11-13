import { describe, expect, it } from 'vitest';
import { withMutationRecord } from './clientToolOutcome';

describe('PowerPoint native tool outcomes', () => {
  it('trusts executor-named verification targets so a pure reorder or deletion needs no review', () => {
    const mutation = withMutationRecord({ ok: true, mutationEvidence: {
      outcome: 'applied',
      native_mutation_started: true,
      affected_slide_ids: ['moved-1', 'edited-1'],
      deleted_slide_ids: ['gone-1'],
      verification_targets: ['edited-1'],
    } }).mutation;
    expect(mutation?.verification_targets).toEqual(['edited-1']);
    expect(mutation?.deleted_slide_ids).toEqual(['gone-1']);
  });

  it('normalizes partial writes and carries slide identities into verification targets', () => {
    expect(withMutationRecord({ ok: false, mutationEvidence: {
      outcome: 'partially_applied',
      native_mutation_started: true,
      last_completed_stage: 'insert_replacement',
      created_slide_ids: ['new-1'],
      affected_slide_ids: ['old-1'],
      slide_id_updates: [{ old_slide_id: 'old-1', new_slide_id: 'new-1' }],
    }, error: {
      code: 'POWERPOINT_ERROR', message: 'delete failed', retryable: false, category: 'host',
    } })).toMatchObject({
      mutation: {
        state: 'partial',
        native_mutation_started: true,
        last_completed_stage: 'insert_replacement',
        created_slide_ids: ['new-1'],
        affected_slide_ids: ['old-1'],
        replacement_slide_ids: { 'old-1': 'new-1' },
        verification_targets: ['new-1', 'old-1'],
        safe_to_retry: false,
      },
    });
  });

  it('surfaces already-resolved slides from a partial-batch failure (2026-09-19 live finding)', () => {
    expect(withMutationRecord({ ok: false, error: {
      code: 'InvalidArgument', message: 'Shape.height', retryable: false, category: 'host',
      details: {
        outcome: 'unknown',
        native_mutation_started: true,
        last_completed_stage: 'slides_completed:0',
        resolved_slides: [
          { slide_ref: 'S11', slide_id: '300', created: true },
          { slide_ref: 'S12', slide_id: '301', created: true },
          { slide_ref: 'S13', slide_id: '302', created: true },
        ],
        created_slide_ids: ['300', '301', '302'],
        affected_slide_ids: ['300', '301', '302'],
      },
    } })).toMatchObject({
      mutation: {
        state: 'uncertain',
        native_mutation_started: true,
        created_slide_ids: ['300', '301', '302'],
        affected_slide_ids: ['300', '301', '302'],
        resolved_slides: [
          { slide_ref: 'S11', slide_id: '300', created: true },
          { slide_ref: 'S12', slide_id: '301', created: true },
          { slide_ref: 'S13', slide_id: '302', created: true },
        ],
      },
    });
  });

  it('does not mark permission or validation failures as safely retryable', () => {
    expect(withMutationRecord({ ok: false, error: {
      code: 'PERMISSION_DENIED', message: 'denied', retryable: false, category: 'host',
      details: { outcome: 'not_applied' },
    } }).mutation).toEqual({
      state: 'not_applied', native_mutation_started: false, safe_to_retry: false,
    });
  });
});
