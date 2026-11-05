/**
 * "@Label" mentions of canvas nodes in the design chat composer.
 *
 * Typing "@" offers the canvas nodes by label; the chosen label is inserted as
 * "@Label". On send, the leader gets the message plus an appendix that maps each
 * mentioned label to its node id and output, so a request such as
 * "@Character 1: 小企鹅 表情更开心" edits exactly that node. The appendix is
 * stripped again wherever the message is shown.
 */
import type { DesignerExecutionGraph } from './executionGraphTypes';

/** First line of the appendix; everything from here on is for the leader only. */
export const DESIGNER_REFS_MARKER = '[[designer-canvas-refs]]';
const MAX_MENTION_OPTIONS = 8;

export type DesignerMentionOption = {
  nodeId: string;
  label: string;
  type: string;
  /** Output URI (file:// path or URL); null while the node has no output. */
  uri: string | null;
};

/** The "@token" the caret is inside, or null. "@" must start the text or follow whitespace. */
export function detectAtToken(value: string, cursor: number): { start: number; query: string } | null {
  const upto = value.slice(0, cursor);
  const at = upto.lastIndexOf('@');
  if (at === -1) return null;
  const before = upto[at - 1];
  if (before !== undefined && !/\s/.test(before)) return null;
  const query = upto.slice(at + 1);
  if (/\s/.test(query)) return null;
  return { start: at, query };
}

function outputUri(node: DesignerExecutionGraph['nodes'][number]): string | null {
  const uri = String(node.output_ref?.uri || '').trim();
  return uri && !uri.startsWith('designer://') ? uri : null;
}

function outputKind(node: DesignerExecutionGraph['nodes'][number]): string {
  return String(node.output_ref?.kind || node.type || '');
}

/** Every labelled node, newest first (most likely what the user wants to refer back to). */
export function mentionOptions(graph: DesignerExecutionGraph | null | undefined): DesignerMentionOption[] {
  const options: DesignerMentionOption[] = [];
  const seen = new Set<string>();
  for (const node of [...(graph?.nodes ?? [])].reverse()) {
    const label = String(node.label || '').trim();
    if (!label || seen.has(label)) continue;
    seen.add(label);
    options.push({ nodeId: node.id, label, type: outputKind(node), uri: outputUri(node) });
  }
  return options;
}

/**
 * The "@query" the caret is in, allowing spaces while the query still starts some
 * label: labels such as "Scene 1: Set_1" contain spaces, so "@Scene 1" must keep
 * the menu open instead of ending the token at the space. Once a label has been
 * chosen ("@Scene 1: Set_1 ...") no label starts with the query and it is closed.
 */
export function detectMentionQuery(
  value: string,
  cursor: number,
  options: DesignerMentionOption[],
): { start: number; query: string } | null {
  const simple = detectAtToken(value, cursor);
  if (simple) return simple;
  const upto = value.slice(0, cursor);
  const at = upto.lastIndexOf('@');
  if (at === -1) return null;
  const before = upto[at - 1];
  if (before !== undefined && !/\s/.test(before)) return null;
  const query = upto.slice(at + 1);
  if (!query.trim() || query.includes('\n')) return null;
  const needle = query.toLowerCase();
  return options.some((item) => item.label.toLowerCase().startsWith(needle)) ? { start: at, query } : null;
}

/** Options whose label contains ``query`` (case-insensitive), at most eight. */
export function filterMentionOptions(options: DesignerMentionOption[], query: string): DesignerMentionOption[] {
  const needle = query.trim().toLowerCase();
  return options.filter((item) => item.label.toLowerCase().includes(needle)).slice(0, MAX_MENTION_OPTIONS);
}

/** ``value`` with the "@query" token at ``token.start`` replaced by "@Label ", plus the caret after it. */
export function insertMention(
  value: string,
  token: { start: number; query: string },
  label: string,
): { value: string; caret: number } {
  const before = value.slice(0, token.start);
  const after = value.slice(token.start + 1 + token.query.length);
  return { value: `${before}@${label} ${after}`, caret: before.length + label.length + 2 };
}

/** Nodes the text mentions as "@Label", in canvas order. Longest label wins ("@Clip 10" over "@Clip 1"). */
export function collectMentionedNodes(
  graph: DesignerExecutionGraph | null | undefined,
  text: string,
): DesignerMentionOption[] {
  if (!graph || !text.includes('@')) return [];
  const options = mentionOptions(graph).sort((a, b) => b.label.length - a.label.length);
  let remaining = text;
  const found = new Set<string>();
  for (const option of options) {
    const token = `@${option.label}`;
    if (!remaining.includes(token)) continue;
    found.add(option.nodeId);
    remaining = remaining.split(token).join(' ');
  }
  return (graph.nodes || [])
    .filter((node) => found.has(node.id))
    .map((node) => ({ nodeId: node.id, label: String(node.label).trim(), type: outputKind(node), uri: outputUri(node) }));
}

export function isPreviewableMention(item: DesignerMentionOption): boolean {
  return Boolean(item.uri) && (item.type === 'image' || item.type === 'video');
}

function displayPath(uri: string): string {
  if (!uri.startsWith('file:')) return uri;
  try {
    let path = decodeURIComponent(new URL(uri).pathname);
    if (/^\/[A-Za-z]:\//.test(path)) path = path.slice(1);
    return path;
  } catch {
    return uri;
  }
}

/** The leader-only appendix naming each mentioned node; empty when nothing is mentioned. */
export function buildMentionAppendix(nodes: DesignerMentionOption[]): string {
  if (nodes.length === 0) return '';
  const lines = nodes.map((node) => {
    const output = node.uri ? `, output=${displayPath(node.uri)}` : ', output=(not generated yet)';
    return `- @${node.label} → node_id=${node.nodeId}, type=${node.type || 'unknown'}${output}`;
  });
  return [
    DESIGNER_REFS_MARKER,
    'The user referenced these canvas nodes with @; apply the request to these node ids:',
    ...lines,
    'Referencing a node with @ together with a change request is the user\'s approval to regenerate it: '
      + 'after updating a node, rerun just that node with swarmflow(node_id=<id>) '
      + '(no cascade) without asking first (no ask_user). Rerun it even if its prompt already '
      + 'contains this change. Skip the rerun only if the user says not to run, '
      + 'or only asked a question about the node.',
  ].join('\n');
}

/** The message the leader receives: the user's text plus the appendix (if any). */
export function withMentionAppendix(text: string, graph: DesignerExecutionGraph | null | undefined): string {
  const appendix = buildMentionAppendix(collectMentionedNodes(graph, text));
  return appendix ? `${text}\n\n${appendix}` : text;
}

/** The user's own text, without a leader-only appendix. */
export function stripMentionAppendix(text: string): string {
  const index = text.indexOf(DESIGNER_REFS_MARKER);
  return index === -1 ? text : text.slice(0, index).trimEnd();
}
