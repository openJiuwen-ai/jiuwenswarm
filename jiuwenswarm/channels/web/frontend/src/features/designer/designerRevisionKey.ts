/** Pure identity helpers for designer canvas revision auto-adoption. */

export type DesignerRevisionIdentity = {
  nodeId: string;
  incoming: ReadonlyArray<{ uri?: string | null } | null | undefined>;
};

function revisionAssetKey(incoming: DesignerRevisionIdentity['incoming']): string {
  const uris = (incoming || [])
    .map((item) => (item && typeof item.uri === 'string' ? item.uri.trim() : ''))
    .filter((uri) => uri.length > 0);
  return Array.from(new Set(uris)).sort().join('|');
}

/**
 * Dedupe key for one auto-adopted canvas revision.
 *
 * `designer.run.start` reuses the graph-lifelong `run_id`, so `run_id:node_id`
 * is identical for every regeneration of the same node and the second round
 * was treated as "already promoted" and never adopted (issue #5326). The key
 * therefore adds the node version bumped by each trigger plus the candidate
 * asset URIs this round produced: a genuine new round always changes it, while
 * re-rendering the same settled run still yields the same key.
 */
export function designerRevisionAdoptionKey(
  runId: string,
  revision: DesignerRevisionIdentity,
  nodeState?: { version?: unknown } | null,
): string {
  const version = typeof nodeState?.version === 'number' && nodeState.version > 0 ? nodeState.version : 0;
  return `${runId}:${revision.nodeId}:v${version}:${revisionAssetKey(revision.incoming)}`;
}
