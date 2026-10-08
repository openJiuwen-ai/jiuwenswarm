import { create } from 'zustand';
import type { DesignerCanvasTool, DesignerDockPanel } from './designerCanvasNodes';

type DesignerUiStore = {
  selectedMaterialId: string;
  viewerOpen: boolean;
  chooserNodeId: string;
  editRequestKey: number;
  canvasTool: DesignerCanvasTool;
  dockPanel: DesignerDockPanel;
  successorMenuNodeId: string | null;
  setCanvasTool: (tool: DesignerCanvasTool) => void;
  setDockPanel: (panel: DesignerDockPanel) => void;
  toggleSuccessorMenu: (nodeId: string) => void;
  closeDock: () => void;
  inspectNode: (nodeId: string, materialIndex?: number) => void;
  startEdit: (materialId: string) => void;
  openViewer: (id: string) => void;
  closeViewer: () => void;
  openRevision: (nodeId: string) => void;
  closeRevision: () => void;
  setSelectedMaterialId: (id: string) => void;
  reset: () => void;
};

const initialState = {
  selectedMaterialId: '',
  viewerOpen: false,
  chooserNodeId: '',
  editRequestKey: 0,
  canvasTool: 'select' as DesignerCanvasTool,
  dockPanel: null as DesignerDockPanel,
  successorMenuNodeId: null as string | null,
};

export const useDesignerUiStore = create<DesignerUiStore>((set) => ({
  ...initialState,

  inspectNode: (nodeId, materialIndex) =>
    set({
      selectedMaterialId:
        typeof materialIndex === 'number' ? `${nodeId}:${materialIndex}` : nodeId,
      viewerOpen: true,
    }),

  startEdit: (materialId) =>
    set((state) => ({
      selectedMaterialId: materialId,
      viewerOpen: true,
      editRequestKey: state.editRequestKey + 1,
    })),

  openViewer: (id) => set({ selectedMaterialId: id, viewerOpen: true }),

  closeViewer: () => set({ viewerOpen: false }),

  openRevision: (nodeId) => set({ chooserNodeId: nodeId }),

  closeRevision: () => set({ chooserNodeId: '' }),

  setSelectedMaterialId: (id) => set({ selectedMaterialId: id }),

  setCanvasTool: (tool) => set({ canvasTool: tool, dockPanel: null, successorMenuNodeId: null }),

  setDockPanel: (panel) =>
    set((state) => ({
      dockPanel: state.dockPanel === panel ? null : panel,
      successorMenuNodeId: null,
    })),

  toggleSuccessorMenu: (nodeId) =>
    set((state) => ({
      successorMenuNodeId: state.successorMenuNodeId === nodeId ? null : nodeId,
      dockPanel: null,
    })),

  closeDock: () => set({ dockPanel: null, successorMenuNodeId: null }),

  reset: () => set({ ...initialState }),
}));
