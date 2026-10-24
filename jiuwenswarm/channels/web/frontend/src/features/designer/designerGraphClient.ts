import { webRequest } from '../../services/webClient';
import type {
  DesignerExecutionGraph,
  DesignerExecutionRun,
  DesignerGraphPatch,
  DesignerMediaCapabilities,
  DesignerGraphSummary,
} from './executionGraphTypes';
import type { DesignerChatMessage } from './designerChatStore';

export type DesignerSessionSummary = {
  session_id: string;
  title: string;
  graph_id: string;
  project_id: string;
  updated_at?: number;
};

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
  sessions?: DesignerSessionSummary[];
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
      { timeoutMs: 60_000 },
    ),
  get: (projectId: string, sessionId?: string) =>
    webRequest<DesignerWorkspace>('designer.workspace.get', {
      project_id: projectId,
      ...(sessionId ? { session_id: sessionId } : {}),
    }),
  createSession: (params: { projectId: string; title?: string }) =>
    webRequest<DesignerWorkspace>('designer.workspace.session.create', {
      project_id: params.projectId,
      ...(params.title ? { title: params.title } : {}),
    }),
  composeSession: (params: {
    projectId: string;
    sessionId: string;
    prompt: string;
    modelName?: string;
    references?: Array<Record<string, unknown>>;
  }) =>
    webRequest<DesignerWorkspace>(
      'designer.workspace.session.compose',
      {
        project_id: params.projectId,
        session_id: params.sessionId,
        prompt: params.prompt,
        ...(params.modelName ? { model_name: params.modelName } : {}),
        ...(params.references?.length ? { references: params.references } : {}),
      },
      { timeoutMs: 60_000 },
    ),
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

  mediaCapabilities: (graphId?: string) =>
    webRequest<DesignerMediaCapabilities>('designer.media.capabilities', graphId ? { graph_id: graphId } : {}),

  startRun: (params: { graphId?: string; runId?: string; nodeId?: string; cascade?: boolean }) =>
    webRequest<{
      run: DesignerExecutionRun;
      warning?: string | null;
      warnings?: string[] | null;
    }>('designer.run.start', {
      ...(params.graphId ? { graph_id: params.graphId } : {}),
      ...(params.runId ? { run_id: params.runId } : {}),
      ...(params.nodeId ? { node_id: params.nodeId } : {}),
      ...(params.cascade ? { cascade: true } : {}),
    }),

  getRun: (params: { runId?: string; graphId?: string }) =>
    webRequest<{ run: DesignerExecutionRun }>('designer.run.get', {
      ...(params.runId ? { run_id: params.runId } : {}),
      ...(params.graphId ? { graph_id: params.graphId } : {}),
    }),

  cancelRun: (runId: string) => webRequest<{ run: DesignerExecutionRun }>('designer.run.cancel', { run_id: runId }),

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
