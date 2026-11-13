export const PRESENTATION_TOOL_NAMES = [
  'read_presentation_context',
  'list_slide_shapes',
  'edit_slide_chart',
  'create_slide_chart',
  'read_chart_style',
  'edit_built_chart',
  'duplicate_slide',
  'import_slides',
  'copy_image_between_slides',
  'edit_slide_master',
  'read_slide_text',
  'read_slide_image',
  'edit_slide_text',
  'edit_slide_xml',
  'verify_slides',
  'verify_slide_visual',
  'find_and_replace',
  'presentation_apply_patch',
] as const;

export type PresentationToolName = (typeof PRESENTATION_TOOL_NAMES)[number];

export interface ClientToolError {
  code: string;
  message: string;
  retryable: boolean;
  category: 'capability' | 'validation' | 'conflict' | 'transport' | 'timeout' | 'host';
  details?: Record<string, unknown>;
}

export interface PresentationMutationRecord {
  state: 'not_applied' | 'applied' | 'partial' | 'uncertain';
  native_mutation_started: boolean;
  last_completed_stage?: string;
  created_slide_ids?: string[];
  deleted_slide_ids?: string[];
  affected_slide_ids?: string[];
  replacement_slide_ids?: Record<string, string>;
  verification_targets?: string[];
  /**
   * Which slide_ref each already resolved to on the live document, in the
   * order they resolved. Present on a partial/uncertain outcome so the
   * caller's next patch can reuse the real slide_id for a slide_ref instead
   * of re-deriving position/role bookkeeping from a stale assumption after
   * a mid-batch failure (2026-09-19 live finding: an unreported orphaned
   * slide desynced the caller's slide_ref → slide_id map from the document).
   */
  resolved_slides?: { slide_ref: string; slide_id: string; created: boolean }[];
  safe_to_retry: boolean;
}

/** Time a tool spent in the host, as the runtime measured it; goes to the answer's ``data``, not to the model. */
export interface HostTimings {
  export_ms?: number;
  insert_ms?: number;
  render_ms?: number;
  sync_count?: number;
}

interface ResponseRecords {
  /** What a write did to the deck, as the runtime saw it; ``withMutationRecord`` turns it into ``mutation``. */
  mutationEvidence?: Record<string, unknown>;
  mutation?: PresentationMutationRecord;
  hostTimings?: HostTimings;
}

/** What a tool runtime returns for one call. */
export type ClientToolResponse = ResponseRecords & (
  | {
    ok: true;
    result?: unknown;
    successFields?: Record<string, unknown>;
    /** A PNG the tool produced (a slide render), returned as an image block. */
    imageBase64?: string;
  }
  | { ok: false; error: ClientToolError }
);
