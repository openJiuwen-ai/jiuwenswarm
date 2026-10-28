import { create } from 'zustand';
import { generateUuidV4 } from '../../utils/uuid';
import type { DesignerExecutionGraph } from './executionGraphTypes';
import {
  extractDesignerGraphPrompt,
  hasDesignerUserPrompt,
  resolveBoundDesignerMessages,
} from './designerChatHistory';
import { extractDesignerGraphReferences, type DesignerStoredReference } from './designerReferences';

export type DesignerChatRole = 'user' | 'assistant' | 'system';

export type DesignerChatMessageKind =
  | 'user'
  | 'thinking'
  | 'bootstrap_done'
  | 'bootstrap_error'
  | 'chat_ack'
  | 'chat_error'
  | 'not_implemented';

export type DesignerChatMessage = {
  id: string;
  role: DesignerChatRole;
  content: string;
  kind: DesignerChatMessageKind;
  createdAt: number;
  references?: DesignerStoredReference[];
};

export type DesignerBootstrapPhase = 'idle' | 'thinking' | 'bootstrapping' | 'done' | 'error';

type DesignerChatStore = {
  activeGraphId: string | null;
  messages: DesignerChatMessage[];
  messagesByGraphId: Record<string, DesignerChatMessage[]>;
  bootstrapPhase: DesignerBootstrapPhase;
  reset: () => void;
  replaceMessages: (graphId: string, messages: DesignerChatMessage[]) => void;
  bindGraph: (graphId: string | null) => void;
  appendMessage: (message: Omit<DesignerChatMessage, 'id' | 'createdAt'> & {
    id?: string;
    createdAt?: number;
  }) => string;
  removeMessage: (id: string) => void;
  setBootstrapPhase: (phase: DesignerBootstrapPhase) => void;
  ensureGraphPrompt: (graph: DesignerExecutionGraph | null | undefined, options?: {
    doneText?: string;
  }) => void;
};

function archiveCurrent(
  activeGraphId: string | null,
  messages: DesignerChatMessage[],
  messagesByGraphId: Record<string, DesignerChatMessage[]>,
): Record<string, DesignerChatMessage[]> {
  if (!activeGraphId) return messagesByGraphId;
  return { ...messagesByGraphId, [activeGraphId]: messages };
}

export const useDesignerChatStore = create<DesignerChatStore>((set, get) => ({
  activeGraphId: null,
  messages: [],
  messagesByGraphId: {},
  bootstrapPhase: 'idle',

  reset: () => {
    const { activeGraphId, messages, messagesByGraphId } = get();
    const archived = archiveCurrent(activeGraphId, messages, messagesByGraphId);
    set({
      messages: [],
      bootstrapPhase: 'idle',
      activeGraphId: null,
      messagesByGraphId: archived,
    });
  },

  replaceMessages: (graphId, messages) => {
    const id = String(graphId || '').trim();
    if (!id) return;
    const next = messages.map((message) => ({ ...message }));
    set((state) => ({
      activeGraphId: id,
      messages: next,
      messagesByGraphId: { ...state.messagesByGraphId, [id]: next },
      bootstrapPhase: 'done',
    }));
  },

  bindGraph: (graphId) => {
    const id = String(graphId ?? '').trim() || null;
    const { activeGraphId, messages, messagesByGraphId } = get();
    if (id === activeGraphId) return;
    const archived = archiveCurrent(activeGraphId, messages, messagesByGraphId);
    const pending = !activeGraphId && messages.length > 0 ? messages : [];
    const nextMessages = resolveBoundDesignerMessages({
      stored: id ? archived[id] : null,
      pending,
    });
    const nextArchive = id
      ? { ...archived, [id]: nextMessages }
      : archived;
    set({
      activeGraphId: id,
      messages: nextMessages,
      messagesByGraphId: nextArchive,
    });
  },

  appendMessage: (message) => {
    const id = message.id ?? generateUuidV4();
    const createdAt = message.createdAt ?? Date.now();
    set((state) => {
      const next = [
        ...state.messages,
        {
          id,
          role: message.role,
          content: message.content,
          kind: message.kind,
          createdAt,
          ...(message.references && message.references.length > 0
            ? { references: message.references }
            : {}),
        },
      ];
      const messagesByGraphId = state.activeGraphId
        ? { ...state.messagesByGraphId, [state.activeGraphId]: next }
        : state.messagesByGraphId;
      return { messages: next, messagesByGraphId };
    });
    return id;
  },

  removeMessage: (id) =>
    set((state) => {
      const next = state.messages.filter((item) => item.id !== id);
      const messagesByGraphId = state.activeGraphId
        ? { ...state.messagesByGraphId, [state.activeGraphId]: next }
        : state.messagesByGraphId;
      return { messages: next, messagesByGraphId };
    }),

  setBootstrapPhase: (phase) => set({ bootstrapPhase: phase }),

  ensureGraphPrompt: (graph, options) => {
    const graphId = String(graph?.graph_id ?? '').trim();
    if (!graphId || !graph) return;
    if (get().activeGraphId !== graphId) {
      get().bindGraph(graphId);
    }
    if (hasDesignerUserPrompt(get().messages)) return;
    const prompt = extractDesignerGraphPrompt(graph);
    const references = extractDesignerGraphReferences(graph);
    if (!prompt && references.length === 0) return;
    get().appendMessage({
      role: 'user',
      content: prompt,
      kind: 'user',
      ...(references.length > 0 ? { references } : {}),
    });
    const doneText = String(options?.doneText ?? '').trim();
    if (!doneText) return;
    get().appendMessage({
      role: 'assistant',
      content: doneText,
      kind: 'bootstrap_done',
    });
    if (get().bootstrapPhase === 'idle') {
      get().setBootstrapPhase('done');
    }
  },
}));
