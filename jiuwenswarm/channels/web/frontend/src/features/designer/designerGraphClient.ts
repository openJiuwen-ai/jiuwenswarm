import { webRequest } from '../../services/webClient';
import type {
  DesignerExecutionGraph,
  DesignerExecutionRun,
  DesignerGraphBootstrapResult,
  DesignerGraphPatch,
  DesignerGraphSummary,
} from './executionGraphTypes';
import type { DesignerChatMessage, DesignerChatMedia } from './designerChatStore';

export type DesignerWorkspace = {
  project: {
    project_id: string;
    name: string;
    project_dir: string;
    work_mode: 'design';
  };
  session: {
    session_id: string;
    title: string;
    project_id: string;
    project_dir: string;
    work_mode: 'design';
  };
  graph: DesignerExecutionGraph;
  messages: DesignerChatMessage[];
};

export const designerWorkspaceClient = {
  create: (params: {
    prompt: string;
    createToken: string;
    modelName?: string;
    references?: Array<Record<string, unknown>>;
  }) =>
    webRequest<DesignerWorkspace>(
      'designer.workspace.create',
      {
        prompt: params.prompt,
        create_token: params.createToken,
        ...(params.modelName ? { model_name: params.modelName } : {}),
        ...(params.references?.length ? { references: params.references } : {}),
      },
      { timeoutMs: 20 * 60 * 1000 },
    ),
  get: (projectId: string) => webRequest<DesignerWorkspace>('designer.workspace.get', { project_id: projectId }),
};

export const designerGraphClient = {
  get: (graphId: string) => webRequest<{ graph: DesignerExecutionGraph }>('designer.graph.get', { graph_id: graphId }),

  list: (projectId?: string) =>
    webRequest<{ graphs: DesignerExecutionGraph[]; summaries?: DesignerGraphSummary[] }>(
      'designer.graph.list',
      projectId ? { project_id: projectId } : {},
    ),

  save: (graph: DesignerExecutionGraph) =>
    webRequest<{ graph: DesignerExecutionGraph }>('designer.graph.save', { graph }),

  patch: (graphId: string, patch: DesignerGraphPatch) =>
    webRequest<{ graph: DesignerExecutionGraph }>('designer.graph.patch', {
      graph_id: graphId,
      patch,
    }),

  chat: (params: {
    graphId: string;
    message: string;
    selectedNodeId?: string;
    runNewNodes?: boolean;
    references?: Array<Record<string, unknown>>;
    history?: Array<{ role: 'user' | 'assistant'; content: string }>;
    /** Outputs the message refers to with "@Label"; persisted with the turn so
     * the bubble can show them again after a reload. */
    media?: DesignerChatMedia[];
  }) =>
    webRequest<{
      graph: DesignerExecutionGraph;
      summary?: string;
      intent?: string;
      run_node_ids?: string[];
      updated_text_uris?: string[];
      run?: DesignerExecutionRun | null;
    }>(
      'designer.graph.chat',
      {
        graph_id: params.graphId,
        message: params.message,
        ...(params.selectedNodeId ? { selected_node_id: params.selectedNodeId } : {}),
        ...(params.runNewNodes ? { run_new_nodes: true } : {}),
        ...(params.references?.length ? { references: params.references } : {}),
        ...(params.history?.length ? { history: params.history } : {}),
        ...(params.media?.length ? { media: params.media } : {}),
      },
      { timeoutMs: 20 * 60 * 1000 },
    ),

  bootstrap: (params: {
    prompt: string;
    title?: string;
    name?: string;
    projectId?: string;
    projectDir?: string;
    workMode?: 'work' | 'code';
    scenario?: string;
    references?: Array<Record<string, unknown>>;
  }) =>
    webRequest<DesignerGraphBootstrapResult>(
      'designer.graph.bootstrap',
      {
        prompt: params.prompt,
        ...(params.title ? { title: params.title } : {}),
        ...(params.name ? { name: params.name } : {}),
        ...(params.projectId ? { project_id: params.projectId } : {}),
        ...(params.projectDir ? { project_dir: params.projectDir } : {}),
        ...(params.workMode ? { work_mode: params.workMode } : {}),
        ...(params.scenario ? { scenario: params.scenario } : {}),
        ...(params.references && params.references.length > 0 ? { references: params.references } : {}),
      },
      { timeoutMs: 20 * 60 * 1000 },
    ),

  startRun: (params: { graphId?: string; runId?: string; nodeId?: string; clearScope?: boolean }) =>
    webRequest<{
      run: DesignerExecutionRun;
      warning?: string | null;
      warnings?: string[] | null;
    }>('designer.run.start', {
      ...(params.graphId ? { graph_id: params.graphId } : {}),
      ...(params.runId ? { run_id: params.runId } : {}),
      ...(params.nodeId ? { node_id: params.nodeId } : {}),
      // The canvas drives the whole remaining pipeline; a run the chat scoped to
      // a single node must not narrow what Execute builds.
      ...(params.clearScope ? { clear_scope: true } : {}),
    }),

  getRun: (params: { runId?: string; graphId?: string }) =>
    webRequest<{ run: DesignerExecutionRun }>('designer.run.get', {
      ...(params.runId ? { run_id: params.runId } : {}),
      ...(params.graphId ? { graph_id: params.graphId } : {}),
    }),

  cancelRun: (runId: string) => webRequest<{ run: DesignerExecutionRun }>('designer.run.cancel', { run_id: runId }),

  chooseOutput: (params: { runId: string; nodeId: string; choice: 'original' | 'new' }) =>
    webRequest<{ run: DesignerExecutionRun }>('designer.run.choose_output', {
      run_id: params.runId,
      node_id: params.nodeId,
      choice: params.choice,
    }),

  listAgentTemplates: () =>
    webRequest<{ templates?: Array<{ id?: string; displayName?: { zh?: string; en?: string } }> }>(
      'agent_templates.list',
      {},
    ),

  getAgentGroup: (id: string) =>
    webRequest<{
      group?: {
        id?: string;
        members?: Array<{
          id?: string;
          displayName?: { zh?: string; en?: string };
        }>;
      };
    }>('agent_groups.show', { id }),
};
