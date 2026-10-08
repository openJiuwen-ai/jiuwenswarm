import { Plus } from 'lucide-react';
import { useCallback, type MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import {
  buildManualDesignerNode,
  positionRightOfNode,
  type DesignerAddTemplate,
} from '../../designerCanvasNodes';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';
import { useComfyuiImport } from '../../useComfyuiImport';
import { DesignerAddNodeMenu } from '../DesignerAddNodeMenu';

type DesignerNodeSuccessorControlProps = {
  nodeId: string;
};

export function DesignerNodeSuccessorControl({ nodeId }: DesignerNodeSuccessorControlProps) {
  const { t } = useTranslation();
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const addNode = useDesignerStore((state) => state.addNode);
  const canvasTool = useDesignerUiStore((state) => state.canvasTool);
  const successorMenuNodeId = useDesignerUiStore((state) => state.successorMenuNodeId);
  const toggleSuccessorMenu = useDesignerUiStore((state) => state.toggleSuccessorMenu);
  const closeDock = useDesignerUiStore((state) => state.closeDock);
  const menuOpen = successorMenuNodeId === nodeId;
  const importComfyui = useComfyuiImport();

  const onToggle = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      event.preventDefault();
      toggleSuccessorMenu(nodeId);
    },
    [nodeId, toggleSuccessorMenu],
  );

  const onPick = useCallback(
    (template: DesignerAddTemplate) => {
      const source = domainGraph?.nodes.find((node) => node.id === nodeId);
      if (!domainGraph || !source) return;
      const node = buildManualDesignerNode({
        template,
        existing: domainGraph.nodes,
        position: positionRightOfNode(source, domainGraph.nodes),
      });
      addNode(node);
      closeDock();
    },
    [addNode, closeDock, domainGraph, nodeId],
  );

  const onImportComfyui = useCallback(
    (files: File[]) => {
      const source = domainGraph?.nodes.find((node) => node.id === nodeId);
      if (!domainGraph || !source) return;
      const origin = positionRightOfNode(source, domainGraph.nodes);
      closeDock();
      void importComfyui(files, origin);
    },
    [closeDock, domainGraph, importComfyui, nodeId],
  );

  if (canvasTool === 'hand') return null;

  return (
    <div className="designer-node__successor-wrap nodrag nopan">
      <button
        type="button"
        className={`designer-node__successor nodrag nopan${menuOpen ? ' is-open' : ''}`}
        aria-label={t('designer.nodeActions.add')}
        title={t('designer.nodeActions.add')}
        aria-expanded={menuOpen}
        data-testid="designer-node-add"
        onClick={onToggle}
        onMouseDown={(event) => event.stopPropagation()}
        onPointerDown={(event) => event.stopPropagation()}
      >
        <Plus size={14} strokeWidth={2.25} aria-hidden />
      </button>
      {menuOpen ? (
        <div
          className="designer-node__successor-menu nodrag nopan nowheel"
          data-testid="designer-node-add-menu"
          onClick={(event) => event.stopPropagation()}
          onMouseDown={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
        >
          <DesignerAddNodeMenu
            title={t('designer.nodeActions.addTitle')}
            testIdPrefix="designer-node-add"
            onPick={onPick}
            onImportComfyui={onImportComfyui}
          />
        </div>
      ) : null}
    </div>
  );
}
