import {
  FolderOpen,
  Hand,
  MousePointer2,
  Upload,
} from 'lucide-react';
import { useCallback, useRef, type ChangeEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useReactFlow } from '@xyflow/react';
import {
  DESIGNER_ADD_TEMPLATES,
  buildManualDesignerNode,
  offsetCanvasPosition,
  type DesignerAddTemplate,
} from '../designerCanvasNodes';
import { ComfyuiGenerationIcon, DesignerAddNodeTypeIcon } from '../designerIcons';
import {
  DESIGNER_UPLOAD_ACCEPT,
  buildUploadedDesignerNode,
  storeDesignerUpload,
  uploadKindFromMime,
} from '../designerNodeUpload';
import { useDesignerGraphEditingLocked } from '../useDesignerGraphEditingLocked';
import { useDesignerRunStore } from '../designerRunStore';
import { useDesignerStore } from '../designerStore';
import { useDesignerUiStore } from '../designerUiStore';
import { useComfyuiImport } from '../useComfyuiImport';

/** Dock exposes image + video generation; audio stays for asset drops only. */
const DOCK_ADD_TEMPLATES = DESIGNER_ADD_TEMPLATES.filter((item) => item.type !== 'audio');

export function DesignerCanvasDock() {
  const { t } = useTranslation();
  const { screenToFlowPosition } = useReactFlow();
  const addNode = useDesignerStore((state) => state.addNode);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const canvasTool = useDesignerUiStore((state) => state.canvasTool);
  const assetsSidebarOpen = useDesignerUiStore((state) => state.assetsSidebarOpen);
  const setCanvasTool = useDesignerUiStore((state) => state.setCanvasTool);
  const toggleAssetsSidebar = useDesignerUiStore((state) => state.toggleAssetsSidebar);
  const closeDock = useDesignerUiStore((state) => state.closeDock);
  const editingLocked = useDesignerGraphEditingLocked();
  const importComfyui = useComfyuiImport();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadInputRef = useRef<HTMLInputElement>(null);

  const canvasCenter = useCallback(() => {
    const pane = document.querySelector('.designer-page__canvas');
    const rect = pane?.getBoundingClientRect();
    return screenToFlowPosition({
      x: (rect?.left ?? 0) + (rect?.width ?? 640) / 2,
      y: (rect?.top ?? 0) + (rect?.height ?? 480) / 2,
    });
  }, [screenToFlowPosition]);

  const placeAndAdd = useCallback(
    (template: DesignerAddTemplate) => {
      if (editingLocked) return;
      const existing = domainGraph?.nodes ?? [];
      const node = buildManualDesignerNode({
        template,
        existing,
        position: offsetCanvasPosition(canvasCenter(), existing.length),
      });
      addNode(node);
      closeDock();
    },
    [addNode, canvasCenter, closeDock, domainGraph?.nodes, editingLocked],
  );

  const placeComfyui = useCallback(
    (files: File[]) => {
      if (editingLocked) return;
      const origin = offsetCanvasPosition(canvasCenter(), domainGraph?.nodes.length ?? 0);
      closeDock();
      void importComfyui(files, origin);
    },
    [canvasCenter, closeDock, domainGraph?.nodes.length, editingLocked, importComfyui],
  );

  const onComfyuiChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files ?? []);
      event.target.value = '';
      if (files.length) placeComfyui(files);
    },
    [placeComfyui],
  );

  const placeUpload = useCallback(
    async (file: File) => {
      const kind = uploadKindFromMime(file.type);
      if (editingLocked || !kind) return;
      const position = offsetCanvasPosition(canvasCenter(), domainGraph?.nodes.length ?? 0);
      closeDock();
      try {
        const stored = await storeDesignerUpload(file);
        const existing = useDesignerStore.getState().domainGraph?.nodes ?? [];
        addNode(buildUploadedDesignerNode({ kind, stored, existing, position }));
      } catch (error) {
        useDesignerRunStore.setState({
          runError: error instanceof Error ? error.message : String(error),
        });
      }
    },
    [addNode, canvasCenter, closeDock, domainGraph?.nodes.length, editingLocked],
  );

  const onUploadChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (file) void placeUpload(file);
    },
    [placeUpload],
  );

  return (
    <div className="designer-canvas-dock" data-testid="designer-canvas-dock">
      <div className="designer-canvas-dock__bar" role="toolbar" aria-label={t('designer.dock.label')}>
        {DOCK_ADD_TEMPLATES.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`designer-canvas-dock__btn${editingLocked ? ' designer-edit-control' : ''}`}
            disabled={editingLocked}
            aria-label={t(`designer.dock.node.${item.id}`)}
            title={t(`designer.dock.node.${item.id}`)}
            data-testid={`designer-canvas-dock-add-${item.id}`}
            onClick={() => placeAndAdd(item)}
          >
            <DesignerAddNodeTypeIcon type={item.type} />
          </button>
        ))}
        <button
          type="button"
          className={`designer-canvas-dock__btn${editingLocked ? ' designer-edit-control' : ''}`}
          disabled={editingLocked}
          aria-label={t('designer.dock.node.comfyui')}
          title={t('designer.dock.comfyuiHint')}
          data-testid="designer-canvas-dock-add-comfyui"
          onClick={() => {
            if (editingLocked) return;
            fileInputRef.current?.click();
          }}
        >
          <ComfyuiGenerationIcon aria-hidden />
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".json,application/json"
          multiple
          hidden
          data-testid="designer-canvas-dock-comfyui-input"
          onChange={onComfyuiChange}
        />
        <span className="designer-canvas-dock__split" aria-hidden />
        <button
          type="button"
          className={`designer-canvas-dock__btn${canvasTool === 'select' ? ' is-active' : ''}`}
          aria-label={t('designer.dock.select')}
          title={t('designer.dock.selectHint')}
          aria-pressed={canvasTool === 'select'}
          data-testid="designer-canvas-dock-select"
          onClick={() => setCanvasTool('select')}
        >
          <MousePointer2 size={18} aria-hidden />
        </button>
        <button
          type="button"
          className={`designer-canvas-dock__btn${canvasTool === 'hand' ? ' is-active' : ''}`}
          aria-label={t('designer.dock.hand')}
          title={t('designer.dock.handHint')}
          aria-pressed={canvasTool === 'hand'}
          data-testid="designer-canvas-dock-hand"
          onClick={() => setCanvasTool('hand')}
        >
          <Hand size={18} aria-hidden />
        </button>
        <span className="designer-canvas-dock__split" aria-hidden />
        <button
          type="button"
          className={`designer-canvas-dock__btn${assetsSidebarOpen ? ' is-active' : ''}`}
          aria-label={t('designer.dock.assets')}
          title={t('designer.dock.assetsHint')}
          aria-pressed={assetsSidebarOpen}
          data-testid="designer-canvas-dock-assets-btn"
          onClick={toggleAssetsSidebar}
        >
          <FolderOpen size={18} aria-hidden />
        </button>
        <button
          type="button"
          className={`designer-canvas-dock__btn${editingLocked ? ' designer-edit-control' : ''}`}
          disabled={editingLocked}
          aria-label={t('designer.dock.upload')}
          title={t('designer.dock.uploadHint')}
          data-testid="designer-canvas-dock-upload-btn"
          onClick={() => {
            if (editingLocked) return;
            uploadInputRef.current?.click();
          }}
        >
          <Upload size={18} aria-hidden />
        </button>
        <input
          ref={uploadInputRef}
          type="file"
          accept={DESIGNER_UPLOAD_ACCEPT}
          hidden
          data-testid="designer-canvas-dock-upload-input"
          onChange={onUploadChange}
        />
      </div>
    </div>
  );
}
