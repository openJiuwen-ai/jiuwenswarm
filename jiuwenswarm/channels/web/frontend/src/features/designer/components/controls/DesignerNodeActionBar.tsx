import { Download, Replace, ScanSearch, Trash } from 'lucide-react';
import { useCallback, useRef, useState, type ChangeEvent, type MouseEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { downloadPreviewFile } from '../../../../components/ui/FilePreviewPanel/filePreviewShared';
import {
  collectDesignerMaterials,
  designerMaterialFilename,
  previewMaterialForNode,
} from '../../designerMaterials';
import {
  canReplaceUploadedNode,
  storeDesignerUpload,
  uploadAcceptForNodeType,
  uploadKindFromMime,
  uploadOutputRef,
  uploadedNodeConfig,
} from '../../designerNodeUpload';
import { isTextLikeNodeType } from '../../mediaNodeConfig';
import { useDesignerGraphEditingLocked } from '../../useDesignerGraphEditingLocked';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';

type DesignerNodeActionBarProps = {
  nodeId: string;
  nodeType: string;
};

export function DesignerNodeActionBar({ nodeId, nodeType }: DesignerNodeActionBarProps) {
  const { t } = useTranslation();
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const run = useDesignerRunStore((state) => state.run);
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);
  const setNodeOutputRef = useDesignerStore((state) => state.setNodeOutputRef);
  const applyUploadedOutput = useDesignerRunStore((state) => state.applyUploadedOutput);
  const canReplace = canReplaceUploadedNode(domainGraph?.nodes.find((node) => node.id === nodeId));
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const [replacing, setReplacing] = useState(false);
  const openViewer = useDesignerUiStore((state) => state.openViewer);
  const requestDeleteNodes = useDesignerUiStore((state) => state.requestDeleteNodes);
  const editingLocked = useDesignerGraphEditingLocked();
  const previewLabel = isTextLikeNodeType(nodeType)
    ? t('designer.nodeActions.previewOrEdit')
    : t('designer.nodeActions.preview');

  const materials = collectDesignerMaterials(domainGraph, run);
  const material = previewMaterialForNode(materials, nodeId, nodeType);
  const hasOutput = Boolean(material);

  const onPreview = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      if (!hasOutput || !material) return;
      openViewer(material.id);
    },
    [hasOutput, material, openViewer],
  );

  const onDownload = useCallback(
    async (event: MouseEvent) => {
      event.stopPropagation();
      if (!material || !hasOutput) return;
      const filename = designerMaterialFilename(material, `designer-node-${nodeId}`);
      if (material.previewUrl) {
        await downloadPreviewFile({ filename, downloadUrl: material.previewUrl });
        return;
      }
      if (!material.textUrl) return;
      try {
        const response = await fetch(material.textUrl);
        if (!response.ok) return;
        const content = await response.text();
        await downloadPreviewFile({ filename, content });
      } catch {
        // ignore fetch/download failures
      }
    },
    [hasOutput, material, nodeId],
  );

  const onReplaceChange = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (editingLocked || !file || uploadKindFromMime(file.type) !== nodeType) return;
      setReplacing(true);
      try {
        const stored = await storeDesignerUpload(file);
        const outputRef = uploadOutputRef(nodeType, stored);
        updateNodeConfig(nodeId, (config) => uploadedNodeConfig(config, stored));
        setNodeOutputRef(nodeId, outputRef);
        applyUploadedOutput(nodeId, outputRef);
      } catch (error) {
        useDesignerRunStore.setState({
          runError: error instanceof Error ? error.message : String(error),
        });
      } finally {
        setReplacing(false);
      }
    },
    [applyUploadedOutput, editingLocked, nodeId, nodeType, setNodeOutputRef, updateNodeConfig],
  );

  const onDelete = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      event.preventDefault();
      if (editingLocked) return;
      requestDeleteNodes([nodeId]);
    },
    [editingLocked, nodeId, requestDeleteNodes],
  );

  return (
    <div
      className="designer-node-action-bar nodrag nopan nowheel"
      role="toolbar"
      aria-label={t('designer.nodeActions.actionBarLabel')}
      data-testid="designer-node-action-bar"
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
    >
      <button
        type="button"
        className="designer-node-action-bar__btn"
        data-testid="designer-node-action-preview"
        disabled={!hasOutput}
        onClick={onPreview}
      >
        <ScanSearch size={14} aria-hidden />
        <span>{previewLabel}</span>
      </button>
      <button
        type="button"
        className="designer-node-action-bar__btn"
        data-testid="designer-node-action-download"
        disabled={!hasOutput}
        onClick={(event) => {
          void onDownload(event);
        }}
      >
        <Download size={14} aria-hidden />
        <span>{t('designer.nodeActions.download')}</span>
      </button>
      {canReplace ? (
        <>
          <button
            type="button"
            data-testid="designer-node-action-replace"
            title={t('designer.nodeActions.replaceHint')}
            disabled={replacing || editingLocked}
            className={`designer-node-action-bar__btn${editingLocked ? ' designer-edit-control' : ''}`}
            onClick={(event) => {
              event.stopPropagation();
              if (editingLocked) return;
              replaceInputRef.current?.click();
            }}
          >
            <Replace size={14} aria-hidden />
            <span>{t('designer.nodeActions.replace')}</span>
          </button>
          <input
            ref={replaceInputRef}
            type="file"
            accept={uploadAcceptForNodeType(nodeType)}
            hidden
            data-testid="designer-node-action-replace-input"
            onChange={(event) => {
              void onReplaceChange(event);
            }}
          />
        </>
      ) : null}
      <button
        type="button"
        className={`designer-node-action-bar__btn${editingLocked ? ' designer-edit-control' : ''}`}
        data-testid="designer-node-action-delete"
        aria-label={t('designer.nodeActions.delete')}
        title={t('designer.nodeActions.deleteHint')}
        disabled={editingLocked}
        onClick={onDelete}
      >
        <Trash size={14} aria-hidden />
        <span>{t('designer.nodeActions.delete')}</span>
      </button>
    </div>
  );
}
