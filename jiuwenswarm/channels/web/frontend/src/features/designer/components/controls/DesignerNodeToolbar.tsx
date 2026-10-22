import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useReactFlow } from '@xyflow/react';
import SendPlaneIcon from '../../../../assets/send_plane.svg?react';
import { isComfyuiNodeConfig } from '../../comfyuiWorkflow';
import { hasPendingDesignerRevision } from '../../designerMaterials';
import { designerToolbarRevealShift } from '../../designerToolbarVisibility';
import { useDesignerGraphEditingLocked } from '../../useDesignerGraphEditingLocked';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';
import {
  isMediaNodeType,
  isUploadedMediaNode,
  readMediaConfig,
  writeMediaGeneratePatch,
} from '../../mediaNodeConfig';
import type { DesignerComfyuiConfig } from '../../executionGraphTypes';
import { DesignerComfyuiParamsForm } from './DesignerComfyuiParamsForm';
import { DesignerMaterialStrip } from './DesignerMaterialStrip';
import { DesignerMediaSettingsMenu } from './DesignerMediaSettingsMenu';

type DesignerNodeToolbarProps = {
  nodeId: string;
  nodeType: string;
};

export function DesignerNodeToolbar({ nodeId, nodeType }: DesignerNodeToolbarProps) {
  const { t } = useTranslation();
  const isMedia = isMediaNodeType(nodeType);
  const uploadedMedia = useDesignerStore((state) => {
    const node = state.domainGraph?.nodes.find((item) => item.id === nodeId);
    return isMedia && Boolean(node && isUploadedMediaNode(node));
  });
  const canGenerate = nodeType !== 'audio' && !uploadedMedia;
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);
  const rerunNode = useDesignerRunStore((state) => state.rerunNode);
  const isRunning = useDesignerRunStore((state) => state.isRunning);
  const editingLocked = useDesignerGraphEditingLocked();
  const nodeState = useDesignerRunStore((state) => state.nodeStates[nodeId]);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const openRevision = useDesignerUiStore((state) => state.openRevision);
  const config = useDesignerStore(
    (state) => state.domainGraph?.nodes.find((node) => node.id === nodeId)?.config ?? {},
  );
  const media = readMediaConfig(config, nodeType);
  const comfyui =
    isMedia && isComfyuiNodeConfig(config)
      ? ((config as { comfyui?: DesignerComfyuiConfig }).comfyui ?? null)
      : null;
  const promptFocusedRef = useRef(false);
  const storePrompt = media.generate?.prompt ?? '';
  const [promptDraft, setPromptDraft] = useState(storePrompt);

  useEffect(() => {
    promptFocusedRef.current = false;
    setPromptDraft(storePrompt);
  }, [nodeId, storePrompt]);

  const pendingRevision = hasPendingDesignerRevision(nodeState);

  const rootRef = useRef<HTMLDivElement>(null);
  const { getViewport, setViewport } = useReactFlow();
  // The toolbar sits below the node inside the clipped canvas: a node near the
  // bottom edge would hide its prompt panel, so pan just enough to reveal it.
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      const root = rootRef.current;
      const flow = root?.closest('.react-flow');
      if (!root || !flow) return;
      const rect = root.getBoundingClientRect();
      const bounds = flow.getBoundingClientRect();
      let bottom = bounds.bottom;
      const dock = flow.parentElement?.querySelector('.designer-canvas-dock')?.getBoundingClientRect();
      if (dock && dock.height > 0 && dock.left < rect.right && dock.right > rect.left) {
        bottom = Math.min(bottom, dock.top);
      }
      const shift = designerToolbarRevealShift(rect, { top: bounds.top, bottom });
      if (shift <= 0) return;
      const viewport = getViewport();
      void setViewport({ ...viewport, y: viewport.y - shift }, { duration: 200 });
    });
    return () => cancelAnimationFrame(frame);
  }, [getViewport, nodeId, setViewport]);

  const patchGenerate = useCallback(
    (patch: Parameters<typeof writeMediaGeneratePatch>[1]) => {
      if (editingLocked) return;
      updateNodeConfig(nodeId, (current) => writeMediaGeneratePatch(current, patch));
    },
    [editingLocked, nodeId, updateNodeConfig],
  );

  const onGenerateNode = useCallback(() => {
    if (!domainGraph || isRunning || editingLocked) return;
    void rerunNode(domainGraph, nodeId);
  }, [domainGraph, editingLocked, isRunning, nodeId, rerunNode]);

  // Audio no longer has upload; only revision compare remains when needed.
  if (isMedia && !canGenerate && !pendingRevision) {
    return null;
  }

  if (!isMedia) {
    return null;
  }

  return (
    <div
      ref={rootRef}
      className="designer-node-toolbar nodrag nopan nowheel"
      data-testid="designer-node-toolbar"
      data-node-type={nodeType}
      data-expanded={canGenerate ? 'generate' : 'idle'}
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onWheel={(event) => event.stopPropagation()}
    >
      {pendingRevision ? (
        <button
          type="button"
          className="designer-node-toolbar__compare"
          data-testid="designer-node-toolbar-tab-compare"
          onClick={() => openRevision(nodeId)}
        >
          {t('designer.revision.compare')}
        </button>
      ) : null}

      {canGenerate ? (
        <div
          className="designer-node-toolbar__panel"
          role="dialog"
          aria-label={t('designer.toolbar.regenerate')}
          data-testid="designer-node-toolbar-panel-generate"
        >
          <DesignerMaterialStrip nodeId={nodeId} nodeType={nodeType} />
          <p className="designer-node-toolbar__prompt-hint" data-testid="designer-node-toolbar-prompt-hint">
            {comfyui ? t('designer.comfyui.promptHint') : t('designer.toolbar.promptHint')}
          </p>
          {comfyui ? <DesignerComfyuiParamsForm nodeId={nodeId} comfyui={comfyui} /> : null}
          <div className="designer-node-toolbar__composer">
            <textarea
              className={`designer-node-toolbar__prompt nodrag nopan nowheel${editingLocked ? ' designer-edit-control' : ''}`}
              readOnly={editingLocked}
              value={promptDraft}
              placeholder={t('designer.toolbar.promptPlaceholder')}
              rows={4}
              data-testid="designer-node-toolbar-prompt"
              onFocus={() => {
                promptFocusedRef.current = true;
              }}
              onBlur={() => {
                promptFocusedRef.current = false;
                const latestConfig =
                  useDesignerStore.getState().domainGraph?.nodes.find((node) => node.id === nodeId)
                    ?.config ?? {};
                setPromptDraft(readMediaConfig(latestConfig, nodeType).generate?.prompt ?? '');
              }}
              onChange={(event) => {
                if (editingLocked) return;
                const value = event.target.value;
                setPromptDraft(value);
                patchGenerate({ prompt: value });
              }}
              onWheel={(event) => event.stopPropagation()}
            />
            <div className="designer-node-toolbar__composer-actions">
              {!comfyui && (nodeType === 'image' || nodeType === 'video') ? (
                <DesignerMediaSettingsMenu nodeId={nodeId} nodeType={nodeType} />
              ) : null}
              <button
                type="button"
                className={`designer-node-toolbar__send${editingLocked ? ' designer-edit-control' : ''}`}
                data-testid="designer-node-toolbar-generate-action"
                disabled={isRunning || !domainGraph || editingLocked}
                title={t('designer.toolbar.rerunHint')}
                aria-label={t('designer.toolbar.generateAction')}
                onClick={onGenerateNode}
              >
                <SendPlaneIcon className="designer-node-toolbar__send-icon" aria-hidden />
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
