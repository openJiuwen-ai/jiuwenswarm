import { useWorkspaceStore } from '../../stores';
import { useDesignerStore } from './designerStore';
import { useDesignerChatStore, type DesignerChatMedia } from './designerChatStore';
import { isPlaceholderAsset } from './designerAssetUrl';
import type { DesignerExecutionGraph } from './executionGraphTypes';
import { designerGraphClient } from './designerGraphClient';
import { useDesignerRunStore } from './designerRunStore';
import type { DesignerBootstrapReference, DesignerStoredReference } from './designerReferences';

export const DESIGNER_BOOTSTRAP_THINKING_MS = 0;

/** True when the message is a new film/design brief (not a small canvas edit). */
export function isNewDesignerBrief(text: string): boolean {
  const t = String(text || '').trim();
  if (t.length < 48) return false;
  return (
    /\b(video|film|story|shot|scene|valentine|create|make|second|vertical|keyframe|cartoon|sequence|storyboard)\b/i.test(
      t,
    ) || /视频|分镜|短片|情人节|镜头|创作|帮我/.test(t)
  );
}

const BOOTSTRAP_STORYBOARD_MAX_CHARS = 3600;

/** One GFM table cell: escape pipes and flatten newlines so a value can never
 * break out of its column or row. Mirrors ``_md_cell`` on the server. */
function mdCell(value: unknown): string {
  return String(value ?? '')
    .replace(/\|/g, '\\|')
    .replace(/\s+/g, ' ')
    .trim();
}

/** Render the sync'd shots as a GFM table — the same structured data the canvas
 * uses. Mirrors ``_storyboard_table_markdown`` on the server. */
function storyboardTable(shots: Array<Record<string, unknown>>): string {
  const rows = shots.map((shot, index) => {
    const no = mdCell(shot.shot_index ?? index + 1) || String(index + 1);
    return `| ${no} | ${mdCell(shot.timeline)} | ${mdCell(shot.setting_id)} | ${mdCell(
      shot.action ?? shot.character_action,
    )} | ${mdCell(shot.camera)} |`;
  });
  if (rows.length === 0) return '';
  return ['| 镜头 | 时间 | 场景 | 画面与动作 | 运镜 |', '| --- | --- | --- | --- | --- |', ...rows].join('\n');
}

/** Director's brief/storyboard LLM calls already ran inside the bootstrap RPC (brief →
 * storyboard → execution graph → validate, all server-side) before this ever returns — the
 * result just wasn't being shown. Surface it in chat instead of a generic placeholder, same
 * spirit as Director Mode's Edit-tab chat answering in one turn with the real plan. */
function buildBootstrapSummaryMessage(graph: {
  metadata?: Record<string, unknown>;
}): string {
  const metadata = graph.metadata || {};
  const scriptAnalysis = (metadata.script_analysis as Record<string, unknown>) || {};
  const characters = Array.isArray(scriptAnalysis.characters) ? (scriptAnalysis.characters as Array<Record<string, unknown>>) : [];
  const castNames = characters
    .map((c) => String(c?.name ?? c?.id ?? '').trim())
    .filter((name) => name.length > 0);
  const shots = (Array.isArray(scriptAnalysis.shots) ? scriptAnalysis.shots : []).filter(
    (shot): shot is Record<string, unknown> => Boolean(shot && typeof shot === 'object'),
  );
  let storyboardMd = String(metadata.approved_storyboard ?? '').trim();
  if (storyboardMd.length > BOOTSTRAP_STORYBOARD_MAX_CHARS) {
    storyboardMd = `${storyboardMd.slice(0, BOOTSTRAP_STORYBOARD_MAX_CHARS).trimEnd()}\n\n…（画布上可查看完整分镜）`;
  }

  const lines = ['✅ 分镜脚本已完成。'];
  const headerBits: string[] = [];
  if (castNames.length > 0) headerBits.push(`角色：${castNames.join('、')}`);
  if (shots.length > 0) headerBits.push(`共 ${shots.length} 个镜头`);
  if (headerBits.length > 0) lines.push(headerBits.join('，'));
  const table = storyboardTable(shots);
  if (table) {
    lines.push('');
    lines.push(table);
  } else if (storyboardMd) {
    lines.push('');
    lines.push(storyboardMd);
  }
  lines.push('');
  lines.push('分镜没问题的话回复「确认」或「生成角色」即可开始角色设定图；想调整分镜就直接告诉我要改哪里。');
  return lines.join('\n');
}

export type LaunchDesignerFromTaskParams = {
  prompt: string;
  projectId?: string;
  projectDir?: string;
  workMode?: 'work' | 'code';
  scenario?: string;
  references?: DesignerBootstrapReference[];
  /** Navigate to Design nav before/while bootstrap runs. */
  onNavigateToDesign: () => void;
  thinkingMs?: number;
  thinkingText?: string;
  doneText?: string;
  errorText?: string;
};

/**
 * Design canvas Assistant send: already on Design page, no nav jump; bootstrap onto canvas.
 * Shares the same ``designer.graph.bootstrap`` path as the Tasks entry.
 */
export async function bootstrapDesignerFromChat(params: {
  prompt: string;
  projectId?: string;
  projectDir?: string;
  workMode?: 'work' | 'code';
  scenario?: string;
  references?: DesignerBootstrapReference[];
  thinkingText?: string;
  doneText?: string;
  errorText?: string;
}): Promise<void> {
  await launchDesignerFromTask({
    ...params,
    onNavigateToDesign: () => undefined,
    thinkingMs: 400,
  });
}

/**
 * Tasks page Design arm → Design tab: compose agentic graph via bootstrap RPC.
 */
export async function launchDesignerFromTask(params: LaunchDesignerFromTaskParams): Promise<void> {
  const prompt = params.prompt.trim();
  const references = params.references || [];
  if (!prompt && references.length === 0) return;

  const thinkingText = params.thinkingText ?? 'Decomposing your request into an agentic design graph…';
  const doneText = params.doneText ?? 'Director composed the workflow. Tweak nodes or hit Play when ready.';
  const errorText = params.errorText ?? 'Failed to compose the design workflow. Please retry.';

  const designerStore = useDesignerStore.getState();
  const chatStore = useDesignerChatStore.getState();

  chatStore.reset();
  designerStore.beginBootstrapEntry(prompt);
  useDesignerRunStore.getState().resetForGraph(useDesignerStore.getState().domainGraph);
  const chatReferences: DesignerStoredReference[] = references.map((item, index) => ({
    kind: item.kind,
    filename: item.filename,
    mime_type: item.mime_type,
    path: item.path,
    uri: item.uri || item.path,
    role: item.role || 'reference',
    order: index + 1,
  }));
  chatStore.appendMessage({
    role: 'user',
    content: prompt,
    kind: 'user',
    ...(chatReferences.length > 0 ? { references: chatReferences } : {}),
  });
  params.onNavigateToDesign();

  chatStore.setBootstrapPhase('thinking');
  const thinkingId = chatStore.appendMessage({
    role: 'assistant',
    content: thinkingText,
    kind: 'thinking',
  });
  useDesignerRunStore.getState().applyLeaderActivity({
    kind: 'thinking',
    text: thinkingText,
    at: Date.now(),
  });
  chatStore.setBootstrapPhase('bootstrapping');

  try {
    const result = await designerGraphClient.bootstrap({
      prompt,
      projectId: params.projectId,
      projectDir: params.projectDir,
      workMode: params.workMode,
      scenario: params.scenario,
      references,
    });
    const graph = result?.graph;
    if (!graph?.graph_id || !Array.isArray(graph.nodes)) {
      throw new Error('bootstrap response missing graph');
    }
    useDesignerStore.getState().applyGraph(graph);
    useDesignerRunStore.getState().applyLeaderActivity(null);
    useDesignerChatStore.getState().removeMessage(thinkingId);
    useDesignerChatStore.getState().bindGraph(graph.graph_id);
    void useWorkspaceStore.getState().loadDesignerGraphs();
    const summary = buildBootstrapSummaryMessage(graph);
    const scenario = String(graph.metadata?.scenario || 'auto');
    const nodeCount = graph.nodes.length;
    useDesignerChatStore.getState().appendMessage({
      role: 'assistant',
      content: summary || `${doneText}\n\nScenario: ${scenario} · Nodes: ${nodeCount}`,
      kind: 'bootstrap_done',
    });
    useDesignerChatStore.getState().setBootstrapPhase('done');
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    useDesignerStore.getState().failBootstrapEntry(message);
    useDesignerChatStore.getState().removeMessage(thinkingId);
    useDesignerChatStore.getState().appendMessage({
      role: 'assistant',
      content: `${errorText}${message ? ` (${message})` : ''}`,
      kind: 'bootstrap_error',
    });
    useDesignerChatStore.getState().setBootstrapPhase('error');
  }
}

/** Last N real turns (user/assistant text only — no thinking/bootstrap noise)
 * already held in the chat store, oldest first, shaped for the chat RPC's
 * ``history`` field. The store is the only place this conversation lives
 * (Designer has no server-side chat history), so this is the one source of
 * "memory" a follow-up turn gets. */
function recentChatHistory(limit = 8): Array<{ role: 'user' | 'assistant'; content: string }> {
  const kept: Array<{ role: 'user' | 'assistant'; content: string }> = [];
  for (const message of useDesignerChatStore.getState().messages) {
    if (message.role !== 'user' && message.role !== 'assistant') continue;
    if (message.kind !== 'user' && message.kind !== 'chat_ack' && message.kind !== 'bootstrap_done') continue;
    const content = message.content.trim();
    if (!content) continue;
    kept.push({ role: message.role, content });
  }
  return kept.slice(-limit);
}

/** Outputs produced by this turn's run, so the reply can show the generated
 * image/video inline — the same one-turn "ask → see the result" behaviour the
 * Edit assistant has. The run has already finished by the time the chat RPC
 * returns, and the adapter merged each completed node's output_ref back onto
 * the graph, so the graph is the source of these URIs. */
function collectGeneratedMedia(
  graph: DesignerExecutionGraph | undefined,
  runNodeIds: string[] | undefined,
): DesignerChatMedia[] {
  if (!graph || !runNodeIds || runNodeIds.length === 0) return [];
  const byId = new Map((graph.nodes || []).map((node) => [node.id, node]));
  const media: DesignerChatMedia[] = [];
  for (const nodeId of runNodeIds) {
    const node = byId.get(nodeId);
    const uri = node?.output_ref?.uri;
    if (!node || !uri || isPlaceholderAsset(uri)) continue;
    media.push({
      nodeId,
      uri,
      kind: String(node.output_ref?.kind || node.type || 'image'),
      ...(node.label ? { label: node.label } : {}),
    });
  }
  return media;
}

/** Node outputs the user pointed at with "@Label" in this message, so the
 * bubble can show the image being referred to. Mirrors the server's
 * ``resolve_label_references``, which feeds the same images to the model as
 * vision references. */
export function collectReferencedMedia(
  graph: DesignerExecutionGraph | null | undefined,
  text: string,
): DesignerChatMedia[] {
  if (!graph || !text.includes('@')) return [];
  const byLabel = new Map<string, DesignerExecutionGraph['nodes'][number]>();
  for (const node of graph.nodes || []) {
    const label = String(node.label || '').trim();
    if (label) byLabel.set(label, node);
  }
  const matched: string[] = [];
  const media: DesignerChatMedia[] = [];
  // Longest label first so "@Character 1" is not shadowed by "@Character".
  for (const label of [...byLabel.keys()].sort((a, b) => b.length - a.length)) {
    if (!text.includes(`@${label}`)) continue;
    if (matched.some((longer) => longer.startsWith(label))) continue;
    matched.push(label);
    const node = byLabel.get(label);
    const uri = node?.output_ref?.uri;
    if (!node || !uri || isPlaceholderAsset(uri)) continue;
    media.push({
      nodeId: node.id,
      uri,
      kind: String(node.output_ref?.kind || node.type || 'image'),
      label,
    });
  }
  return media;
}

export async function chatDesignerGraph(params: {
  graphId: string;
  prompt: string;
  selectedNodeId?: string;
  references?: Array<Record<string, unknown>>;
  thinkingText?: string;
  errorText?: string;
}): Promise<void> {
  const prompt = params.prompt.trim();
  if (!prompt) return;
  const chatStore = useDesignerChatStore.getState();
  const history = recentChatHistory();
  // "@Label" mentions resolve to that node's current output, so the user's own
  // bubble can show the image they referred to.
  const referenced = collectReferencedMedia(useDesignerStore.getState().domainGraph, prompt);
  chatStore.appendMessage({
    role: 'user',
    content: prompt,
    kind: 'user',
    ...(referenced.length > 0 ? { media: referenced } : {}),
  });
  const thinkingId = chatStore.appendMessage({
    role: 'assistant',
    content: params.thinkingText || 'Updating the workflow…',
    kind: 'thinking',
  });
  useDesignerRunStore.getState().applyLeaderActivity({
    kind: 'thinking',
    text: params.thinkingText || 'Updating the workflow…',
    at: Date.now(),
  });
  try {
    await useDesignerStore.getState().flushSave();
    const result = await designerGraphClient.chat({
      graphId: params.graphId,
      message: prompt,
      selectedNodeId: params.selectedNodeId,
      references: params.references,
      history,
      media: referenced,
    });
    chatStore.removeMessage(thinkingId);
    useDesignerRunStore.getState().applyLeaderActivity(null);
    if (result.graph?.graph_id) {
      useDesignerStore.getState().applyGraph(result.graph);
    }
    if (result.run) {
      useDesignerRunStore.getState().applyRun(result.run);
    }
    const media = collectGeneratedMedia(result.graph, result.run_node_ids);
    chatStore.appendMessage({
      role: 'assistant',
      content: String(result.summary || 'Updated the workflow.').trim(),
      kind: 'chat_ack',
      ...(media.length > 0 ? { media } : {}),
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    chatStore.removeMessage(thinkingId);
    chatStore.appendMessage({
      role: 'assistant',
      content: `${params.errorText || 'Could not update the workflow.'}${message ? ` (${message})` : ''}`,
      kind: 'chat_error',
    });
  }
}
