import { create } from 'zustand';
import type {
  DesignerCanvasTool,
  DesignerDockPanel,
  DesignerNodePortSide,
} from './designerCanvasNodes';

export type DesignerNodePortMenu = {
  nodeId: string;
  side: DesignerNodePortSide;
};

export type DesignerZoomKind = 'in' | 'out' | 'fit' | '50' | '100' | '200';

export type DesignerZoomRequest = {
  id: number;
  kind: DesignerZoomKind;
};

type DesignerUiStore = {
  selectedMaterialId: string;
  viewerOpen: boolean;
  editRequestKey: number;
  canvasTool: DesignerCanvasTool;
  dockPanel: DesignerDockPanel;
  nodePortMenu: DesignerNodePortMenu | null;
  chatSidebarOpen: boolean;
  assetsSidebarOpen: boolean;
  minimapVisible: boolean;
  edgesVisible: boolean;
  zoomPercent: number;
  zoomRequest: DesignerZoomRequest | null;
  layoutRequestId: number;
  pendingDeleteNodeIds: string[];
  setCanvasTool: (tool: DesignerCanvasTool) => void;
  setDockPanel: (panel: DesignerDockPanel) => void;
  toggleNodePortMenu: (nodeId: string, side: DesignerNodePortSide) => void;
  closeDock: () => void;
  toggleChatSidebar: () => void;
  setChatSidebarOpen: (open: boolean) => void;
  toggleAssetsSidebar: () => void;
  setAssetsSidebarOpen: (open: boolean) => void;
  toggleMinimap: () => void;
  toggleEdges: () => void;
  setZoomPercent: (percent: number) => void;
  requestZoom: (kind: DesignerZoomKind) => void;
  requestAutoLayout: () => void;
  requestDeleteNodes: (nodeIds: string[]) => void;
  dismissDeleteNodes: () => void;
  inspectNode: (nodeId: string, materialIndex?: number) => void;
  startEdit: (materialId: string) => void;
  openViewer: (id: string) => void;
  closeViewer: () => void;
  setSelectedMaterialId: (id: string) => void;
  reset: () => void;
};

const initialState = {
  selectedMaterialId: '',
  viewerOpen: false,
  editRequestKey: 0,
  canvasTool: 'select' as DesignerCanvasTool,
  dockPanel: null as DesignerDockPanel,
  nodePortMenu: null as DesignerNodePortMenu | null,
  chatSidebarOpen: true,
  assetsSidebarOpen: false,
  minimapVisible: true,
  edgesVisible: true,
  zoomPercent: 100,
  zoomRequest: null as DesignerZoomRequest | null,
  layoutRequestId: 0,
  pendingDeleteNodeIds: [] as string[],
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

  setSelectedMaterialId: (id) => set({ selectedMaterialId: id }),

  setCanvasTool: (tool) => set({ canvasTool: tool, dockPanel: null, nodePortMenu: null }),

  setDockPanel: (panel) =>
    set((state) => ({
      dockPanel: state.dockPanel === panel ? null : panel,
      nodePortMenu: null,
    })),

  toggleNodePortMenu: (nodeId, side) =>
    set((state) => {
      const open =
        state.nodePortMenu?.nodeId === nodeId && state.nodePortMenu.side === side;
      return {
        nodePortMenu: open ? null : { nodeId, side },
        dockPanel: null,
      };
    }),

  closeDock: () => set({ dockPanel: null, nodePortMenu: null }),

  toggleChatSidebar: () => set((state) => ({ chatSidebarOpen: !state.chatSidebarOpen })),

  setChatSidebarOpen: (open) => set({ chatSidebarOpen: open }),

  toggleAssetsSidebar: () => set((state) => ({ assetsSidebarOpen: !state.assetsSidebarOpen })),

  setAssetsSidebarOpen: (open) => set({ assetsSidebarOpen: open }),

  toggleMinimap: () => set((state) => ({ minimapVisible: !state.minimapVisible })),

  toggleEdges: () => set((state) => ({ edgesVisible: !state.edgesVisible })),

  setZoomPercent: (percent) => set({ zoomPercent: percent }),

  requestZoom: (kind) =>
    set((state) => ({
      zoomRequest: { id: (state.zoomRequest?.id ?? 0) + 1, kind },
    })),

  requestAutoLayout: () => set((state) => ({ layoutRequestId: state.layoutRequestId + 1 })),

  requestDeleteNodes: (nodeIds) => {
    const ids = [...new Set(nodeIds.map((id) => id.trim()).filter(Boolean))];
    if (ids.length === 0) return;
    set({ pendingDeleteNodeIds: ids });
  },

  dismissDeleteNodes: () => set({ pendingDeleteNodeIds: [] }),

  reset: () => set({ ...initialState }),
}));
