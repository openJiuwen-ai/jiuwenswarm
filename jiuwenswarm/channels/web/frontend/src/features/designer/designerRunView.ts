import { hasPendingDesignerRevision } from './designerMaterials';
import type { DesignerExecutionRun, DesignerNodeState } from './executionGraphTypes';
import { DESIGNER_RUN_STATUS_FAILED } from './executionGraphTypes';

export const DESIGNER_ACTIVE_RUN_STATUSES = new Set(['running', 'paused']);
export const DESIGNER_TERMINAL_RUN_STATUSES = new Set(['completed', 'failed', 'cancelled']);

export function isActiveDesignerRun(status: string | undefined): boolean {
  return Boolean(status && DESIGNER_ACTIVE_RUN_STATUSES.has(status));
}

/** Prefer run-level error; fall back to the first failed node error. */
export function designerRunFailureMessage(
  run: DesignerExecutionRun | null | undefined,
): string | null {
  if (!run || run.status !== DESIGNER_RUN_STATUS_FAILED) {
    return null;
  }
  const top = String(run.error || '').trim();
  if (top) return top;
  for (const state of Object.values(run.node_states || {})) {
    const message = String(state?.error || '').trim();
    if (message) return message;
  }
  return null;
}

export function nodeStatusFromRun(
  run: DesignerExecutionRun | null | undefined,
  nodeId: string,
): string {
  const state = run?.node_states?.[nodeId] as DesignerNodeState | undefined;
  return state?.status || 'pending';
}

export function mergeRunStatesIntoNodes<
  T extends {
    id: string;
    data: {
      status?: string;
      outputRef?: unknown;
      outputRefs?: unknown;
      error?: string | null;
      pendingRevision?: boolean;
    };
  },
>(nodes: T[], run: DesignerExecutionRun | null | undefined): T[] {
  return nodes.map((node) => {
    const state = run?.node_states?.[node.id];
    return {
      ...node,
      data: {
        ...node.data,
        status: state?.status || node.data.status || 'pending',
        outputRef: state?.output_ref ?? node.data.outputRef,
        outputRefs: state?.output_refs ?? node.data.outputRefs,
        error: state?.error ?? node.data.error ?? null,
        pendingRevision: hasPendingDesignerRevision(state),
      },
    };
  });
}
