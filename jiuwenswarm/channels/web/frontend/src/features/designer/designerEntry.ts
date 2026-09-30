import { useWorkspaceStore } from '../../stores';
import { useDesignerStore } from './designerStore';
import { useDesignerChatStore } from './designerChatStore';
import { designerGraphClient } from './designerGraphClient';
import { DESIGNER_MATERIAL_SAVED_EVENT } from './designerMaterials';
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
    const scenario = String(graph.metadata?.scenario || 'auto');
    const nodeCount = graph.nodes.length;
    useDesignerChatStore.getState().appendMessage({
      role: 'assistant',
      content: `${doneText}\n\nScenario: ${scenario} · Nodes: ${nodeCount}`,
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

export async function chatDesignerGraph(params: {
  graphId: string;
  prompt: string;
  selectedNodeId?: string;
  thinkingText?: string;
  errorText?: string;
}): Promise<void> {
  const prompt = params.prompt.trim();
  if (!prompt) return;
  const chatStore = useDesignerChatStore.getState();
  chatStore.appendMessage({ role: 'user', content: prompt, kind: 'user' });
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
    });
    chatStore.removeMessage(thinkingId);
    useDesignerRunStore.getState().applyLeaderActivity(null);
    if (result.graph?.graph_id) {
      useDesignerStore.getState().applyGraph(result.graph);
    }
    if (result.run) {
      useDesignerRunStore.getState().applyRun(result.run);
    }
    for (const uri of new Set(result.updated_text_uris ?? [])) {
      window.dispatchEvent(new CustomEvent(DESIGNER_MATERIAL_SAVED_EVENT, { detail: { uri } }));
    }
    chatStore.appendMessage({
      role: 'assistant',
      content: String(result.summary || 'Updated the workflow.').trim(),
      kind: 'chat_ack',
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
