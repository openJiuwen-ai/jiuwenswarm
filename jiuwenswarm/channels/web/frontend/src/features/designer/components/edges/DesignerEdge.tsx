import {
  BaseEdge,
  EdgeToolbar,
  getBezierPath,
  type Edge,
  type EdgeProps,
} from '@xyflow/react';
import { Trash2 } from 'lucide-react';
import { useCallback, type MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { useDesignerGraphEditingLocked } from '../../useDesignerGraphEditingLocked';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';

export type DesignerEdgeType = Edge<{ label?: string }>;

export function DesignerEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style,
  markerEnd,
  selected,
}: EdgeProps<DesignerEdgeType>) {
  const { t } = useTranslation();
  const removeEdges = useDesignerStore((state) => state.removeEdges);
  const editingLocked = useDesignerGraphEditingLocked();
  const edgesVisible = useDesignerUiStore((state) => state.edgesVisible);
  const [edgePath, centerX, centerY] = getBezierPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
  });

  const onDelete = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      event.preventDefault();
      if (editingLocked) return;
      removeEdges([id]);
    },
    [editingLocked, id, removeEdges],
  );

  if (!edgesVisible) {
    return (
      <BaseEdge
        id={id}
        path={edgePath}
        style={{ opacity: 0 }}
        interactionWidth={0}
      />
    );
  }

  return (
    <>
      <BaseEdge
        id={id}
        path={edgePath}
        markerEnd={markerEnd}
        style={style}
        interactionWidth={24}
      />
      <EdgeToolbar
        edgeId={id}
        x={centerX}
        y={centerY}
        isVisible={Boolean(selected)}
        className="designer-edge-toolbar-portal"
        data-testid="designer-edge-toolbar"
      >
        <div className="designer-edge-toolbar" role="group">
          <button
            type="button"
            className={`designer-edge-delete${editingLocked ? ' designer-edit-control' : ''}`}
            aria-label={t('designer.edge.delete')}
            title={t('designer.edge.delete')}
            data-testid="designer-edge-delete"
            disabled={editingLocked}
            onClick={onDelete}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <Trash2 size={14} strokeWidth={2.25} aria-hidden />
          </button>
        </div>
      </EdgeToolbar>
    </>
  );
}

export const designerEdgeTypes = {
  designer: DesignerEdge,
};
