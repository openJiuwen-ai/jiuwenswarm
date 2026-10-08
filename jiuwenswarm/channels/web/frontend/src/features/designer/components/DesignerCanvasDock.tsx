import {
  FolderOpen,
  Hand,
  LayoutGrid,
  MousePointer2,
  Plus,
} from 'lucide-react';
import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useReactFlow } from '@xyflow/react';
import { DesignerAddNodeMenu } from './DesignerAddNodeMenu';
import { DesignerAssetsPanel } from './DesignerAssetsPanel';
import {
  buildManualDesignerNode,
  offsetCanvasPosition,
  type DesignerAddTemplate,
} from '../designerCanvasNodes';
import { useDesignerStore } from '../designerStore';
import { useDesignerUiStore } from '../designerUiStore';
import { DESIGNER_FIT_VIEW_PADDING } from '../designerFitView';
import { useComfyuiImport } from '../useComfyuiImport';

export function DesignerCanvasDock() {
  const { t } = useTranslation();
  const { fitView, screenToFlowPosition } = useReactFlow();
  const addNode = useDesignerStore((state) => state.addNode);
  const autoLayout = useDesignerStore((state) => state.autoLayout);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const canvasTool = useDesignerUiStore((state) => state.canvasTool);
  const dockPanel = useDesignerUiStore((state) => state.dockPanel);
  const setCanvasTool = useDesignerUiStore((state) => state.setCanvasTool);
  const setDockPanel = useDesignerUiStore((state) => state.setDockPanel);
  const closeDock = useDesignerUiStore((state) => state.closeDock);
  const importComfyui = useComfyuiImport();

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
      const existing = domainGraph?.nodes ?? [];
      const node = buildManualDesignerNode({
        template,
        existing,
        position: offsetCanvasPosition(canvasCenter(), existing.length),
      });
      addNode(node);
      closeDock();
    },
    [addNode, canvasCenter, closeDock, domainGraph?.nodes],
  );

  const placeComfyui = useCallback(
    (files: File[]) => {
      const origin = offsetCanvasPosition(canvasCenter(), domainGraph?.nodes.length ?? 0);
      closeDock();
      void importComfyui(files, origin);
    },
    [canvasCenter, closeDock, domainGraph?.nodes.length, importComfyui],
  );

  const runAutoLayout = useCallback(() => {
    autoLayout();
    closeDock();
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        void fitView({ padding: DESIGNER_FIT_VIEW_PADDING, duration: 220 });
      });
    });
  }, [autoLayout, closeDock, fitView]);

  return (
    <div className="designer-canvas-dock" data-testid="designer-canvas-dock">
      {dockPanel === 'add' ? (
        <div className="designer-canvas-dock__panel" data-testid="designer-canvas-dock-add">
          <DesignerAddNodeMenu
            title={t('designer.dock.addTitle')}
            testIdPrefix="designer-canvas-add"
            onPick={placeAndAdd}
            onImportComfyui={placeComfyui}
          />
        </div>
      ) : null}

      {dockPanel === 'assets' ? (
        <div
          className="designer-canvas-dock__panel designer-canvas-dock__panel--assets"
          data-testid="designer-canvas-dock-assets"
        >
          <p className="designer-canvas-dock__panel-title">{t('designer.dock.assetsTitle')}</p>
          <DesignerAssetsPanel />
        </div>
      ) : null}

      <div className="designer-canvas-dock__bar" role="toolbar" aria-label={t('designer.dock.label')}>
        <button
          type="button"
          className={`designer-canvas-dock__btn${dockPanel === 'add' ? ' is-active' : ''}`}
          aria-label={t('designer.dock.add')}
          title={t('designer.dock.add')}
          aria-pressed={dockPanel === 'add'}
          data-testid="designer-canvas-dock-add-btn"
          onClick={() => setDockPanel('add')}
        >
          <Plus size={18} aria-hidden />
        </button>
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
        <button
          type="button"
          className="designer-canvas-dock__btn"
          aria-label={t('designer.dock.layout')}
          title={t('designer.dock.layoutHint')}
          data-testid="designer-canvas-dock-layout"
          disabled={!domainGraph?.nodes.length}
          onClick={runAutoLayout}
        >
          <LayoutGrid size={18} aria-hidden />
        </button>
        <span className="designer-canvas-dock__split" aria-hidden />
        <button
          type="button"
          className={`designer-canvas-dock__btn${dockPanel === 'assets' ? ' is-active' : ''}`}
          aria-label={t('designer.dock.assets')}
          title={t('designer.dock.assetsHint')}
          aria-pressed={dockPanel === 'assets'}
          data-testid="designer-canvas-dock-assets-btn"
          onClick={() => setDockPanel('assets')}
        >
          <FolderOpen size={18} aria-hidden />
        </button>
      </div>
    </div>
  );
}
