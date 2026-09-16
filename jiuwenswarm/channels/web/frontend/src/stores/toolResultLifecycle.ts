import { ToolExecutionStatus, ToolResult } from '../types';

export function mergeToolResultProgress(existing: ToolResult | undefined, incoming: ToolResult): ToolResult {
  if (incoming.beamSearch || !existing?.beamSearch) {
    return incoming;
  }
  return {
    ...incoming,
    beamSearch: existing.beamSearch,
  };
}

function hasSameResultData(existing: ToolResult, incoming: ToolResult): boolean {
  return (
    existing.result === incoming.result &&
    existing.success === incoming.success &&
    Boolean(existing.pending) === Boolean(incoming.pending) &&
    (existing.summary || '') === (incoming.summary || '') &&
    existing.beamSearch === incoming.beamSearch
  );
}

export function shouldDropToolResult(currentStatus: ToolExecutionStatus, existing: ToolResult | undefined, incoming: ToolResult): boolean {
  if (incoming.pending && (currentStatus === 'completed' || currentStatus === 'error' || currentStatus === 'timeout')) {
    return true;
  }
  const finalStatus: ToolExecutionStatus = incoming.pending
    ? 'pending'
    : incoming.success ? 'completed' : 'error';
  return currentStatus === finalStatus && existing !== undefined && hasSameResultData(existing, incoming);
}
