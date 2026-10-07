import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { DesignerTextEditor } from '../../DesignerTextEditor';
import { isComfyuiNodeConfig } from '../../comfyuiWorkflow';
import { useDesignerAssetLibraryStore } from '../../designerAssetLibraryStore';
import { localPathToFileUri, uploadDesignerAsset } from '../../designerAssetUrl';
import {
  collectDesignerMaterials,
  hasPendingDesignerRevision,
} from '../../designerMaterials';
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';
import {
  isMediaNodeType,
  isTextLikeNodeType,
  readMediaConfig,
  writeMediaGeneratePatch,
  writeMediaUploadPatch,
} from '../../mediaNodeConfig';
import type { DesignerComfyuiConfig } from '../../executionGraphTypes';
import { DesignerComfyuiParamsForm } from './DesignerComfyuiParamsForm';
import { DesignerMaterialStrip } from './DesignerMaterialStrip';

type DesignerNodeToolbarProps = {
  nodeId: string;
  nodeType: string;
};

type ExpandedPanel = 'generate' | 'upload' | 'edit' | null;

function savedFileForFilename(
  graph: ReturnType<typeof useDesignerStore.getState>['domainGraph'],
  filename: string,
): { uri: string; mime_type?: string } | null {
  const want = filename.trim().toLowerCase();
  if (!want || !graph) return null;
  const candidates: Array<{ uri?: string; filename?: string; label?: string; mime_type?: string }> = [];
  const refs = graph.metadata?.user_references;
  if (Array.isArray(refs)) {
    for (const item of refs) {
      if (item && typeof item === 'object') candidates.push(item as { uri?: string; filename?: string; mime_type?: string });
    }
  }
  for (const node of graph.nodes) {
    const upload = node.config?.upload;
    if (upload?.uri) candidates.push(upload);
    if (node.output_ref?.uri) {
      candidates.push({
        uri: node.output_ref.uri,
        filename: node.output_ref.label,
        mime_type: node.output_ref.mime_type,
      });
    }
    for (const slot of node.config?.materials ?? []) {
      if (slot?.uri) candidates.push(slot);
    }
  }
  for (const item of candidates) {
    const uri = String(item.uri || '').trim();
    if (!uri.startsWith('file:')) continue;
    const name = String(item.filename || item.label || '').trim().toLowerCase();
    const fromUri = decodeURIComponent(uri.split('/').pop() || '').toLowerCase();
    if (name === want || fromUri === want) {
      return { uri, mime_type: item.mime_type };
    }
  }
  return null;
}

export function DesignerNodeToolbar({ nodeId, nodeType }: DesignerNodeToolbarProps) {
  const { t } = useTranslation();
  const isTextLike = isTextLikeNodeType(nodeType);
  const isMedia = isMediaNodeType(nodeType);
  const canGenerate = nodeType !== 'audio';
  const updateNodeConfig = useDesignerStore((state) => state.updateNodeConfig);
  const setNodeOutputRef = useDesignerStore((state) => state.setNodeOutputRef);
  const applyUploadedOutput = useDesignerRunStore((state) => state.applyUploadedOutput);
  const rerunNode = useDesignerRunStore((state) => state.rerunNode);
  const isRunning = useDesignerRunStore((state) => state.isRunning);
  const run = useDesignerRunStore((state) => state.run);
  const nodeState = useDesignerRunStore((state) => state.nodeStates[nodeId]);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const addFromFile = useDesignerAssetLibraryStore((state) => state.addFromFile);
  const getAsset = useDesignerAssetLibraryStore((state) => state.getById);
  const inspectNode = useDesignerUiStore((state) => state.inspectNode);
  const startEdit = useDesignerUiStore((state) => state.startEdit);
  const openRevision = useDesignerUiStore((state) => state.openRevision);
  const config = useDesignerStore(
    (state) => state.domainGraph?.nodes.find((node) => node.id === nodeId)?.config ?? {},
  );
  const media = readMediaConfig(config, nodeType);
  const comfyui =
    isMedia && isComfyuiNodeConfig(config)
      ? ((config as { comfyui?: DesignerComfyuiConfig }).comfyui ?? null)
      : null;
  const fileInputRef = useRef<HTMLInputElement>(null);
  const promptFocusedRef = useRef(false);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [expanded, setExpanded] = useState<ExpandedPanel>(null);
  const storePrompt = media.generate?.prompt ?? '';
  const [promptDraft, setPromptDraft] = useState(storePrompt);

  useEffect(() => {
    setExpanded(null);
    promptFocusedRef.current = false;
  }, [nodeId]);

  useEffect(() => {
    if (expanded !== 'generate') {
      promptFocusedRef.current = false;
      return;
    }
    if (!promptFocusedRef.current) {
      setPromptDraft(storePrompt);
    }
  }, [expanded, nodeId, storePrompt]);

  const materials = collectDesignerMaterials(domainGraph, run);
  const material =
    materials.find((item) => item.id.startsWith(`${nodeId}:`)) ??
    materials.find((item) => item.nodeId === nodeId);
  const hasOutput = Boolean(
    material && !material.placeholder && (material.previewUrl || material.textUrl),
  );
  const pendingRevision = hasPendingDesignerRevision(nodeState);

  const patchGenerate = useCallback(
    (patch: Parameters<typeof writeMediaGeneratePatch>[1]) => {
      updateNodeConfig(nodeId, (current) => writeMediaGeneratePatch(current, patch));
    },
    [nodeId, updateNodeConfig],
  );

  const persistUploadedFile = useCallback(
    async (file: File) => {
      const asset = addFromFile(file);
      if (!asset) return;
      setUploading(true);
      try {
        const stored = await uploadDesignerAsset(file);
        const outputRef = {
          kind: nodeType,
          uri: localPathToFileUri(stored.path),
          mime_type: stored.mime_type || asset.mime_type || file.type,
          label: stored.filename || asset.filename,
        };
        updateNodeConfig(nodeId, (current) => ({
          ...writeMediaUploadPatch(current, {
            filename: outputRef.label,
            asset_id: asset.id,
            mime_type: outputRef.mime_type,
            uri: outputRef.uri,
          }),
          user_replaced_output: true,
        }));
        setNodeOutputRef(nodeId, outputRef);
        applyUploadedOutput(nodeId, outputRef);
        await useDesignerStore.getState().flushSave();
      } catch (error) {
        useDesignerRunStore.setState({
          runError: error instanceof Error ? error.message : String(error),
        });
      } finally {
        setUploading(false);
      }
    },
    [addFromFile, applyUploadedOutput, nodeId, nodeType, setNodeOutputRef, updateNodeConfig],
  );

  const confirmUpload = useCallback(async () => {
    const assetId = media.upload?.asset_id?.trim();
    if (!assetId) return;
    const asset = getAsset(assetId);
    if (!asset) {
      const saved = savedFileForFilename(domainGraph, media.upload?.filename || '');
      if (!saved) {
        fileInputRef.current?.click();
        return;
      }
      const outputRef = {
        kind: nodeType,
        uri: saved.uri,
        mime_type: saved.mime_type || media.upload?.mime_type,
        label: media.upload?.filename,
      };
      updateNodeConfig(nodeId, (current) => ({
        ...writeMediaUploadPatch(current, {
          filename: outputRef.label,
          asset_id: assetId,
          mime_type: outputRef.mime_type,
          uri: outputRef.uri,
        }),
        user_replaced_output: true,
      }));
      setNodeOutputRef(nodeId, outputRef);
      applyUploadedOutput(nodeId, outputRef);
      await useDesignerStore.getState().flushSave();
      return;
    }
    try {
      const blob = await fetch(asset.objectUrl).then((response) => response.blob());
      const file = new File([blob], asset.filename, { type: asset.mime_type || blob.type });
      await persistUploadedFile(file);
    } catch (error) {
      useDesignerRunStore.setState({
        runError: error instanceof Error ? error.message : String(error),
      });
    }
  }, [
    applyUploadedOutput,
    domainGraph,
    getAsset,
    media.upload?.asset_id,
    media.upload?.filename,
    media.upload?.mime_type,
    nodeId,
    nodeType,
    persistUploadedFile,
    setNodeOutputRef,
    updateNodeConfig,
  ]);

  const onFileChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      const file = event.target.files?.[0];
      event.target.value = '';
      if (file) void persistUploadedFile(file);
    },
    [persistUploadedFile],
  );

  const onDrop = useCallback(
    (event: DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      event.stopPropagation();
      setDragging(false);
      const file = event.dataTransfer.files?.[0];
      if (file) void persistUploadedFile(file);
    },
    [persistUploadedFile],
  );

  const canConfirmUpload = Boolean(media.upload?.asset_id?.trim()) && !uploading;

  const onGenerateNode = useCallback(() => {
    if (!domainGraph || isRunning) return;
    void rerunNode(domainGraph, nodeId);
  }, [domainGraph, isRunning, nodeId, rerunNode]);

  const secondaryPanel: ExpandedPanel = isTextLike ? 'edit' : 'upload';

  return (
    <div
      className="designer-node-toolbar nodrag nopan nowheel"
      data-testid="designer-node-toolbar"
      data-node-type={nodeType}
      data-expanded={expanded ?? 'idle'}
      onClick={(event) => event.stopPropagation()}
      onMouseDown={(event) => event.stopPropagation()}
      onWheel={(event) => event.stopPropagation()}
    >
      <div className="designer-node-toolbar__tabs" role="tablist" aria-label={t('designer.toolbar.modeLabel')}>
        <button
          type="button"
          role="tab"
          aria-selected={false}
          className="designer-node-toolbar__tab"
          data-testid="designer-node-toolbar-tab-inspect"
          disabled={!hasOutput}
          onClick={() => inspectNode(nodeId)}
        >
          {t('designer.toolbar.inspect')}
        </button>
        {canGenerate ? (
          <button
            type="button"
            role="tab"
            data-testid="designer-node-toolbar-tab-generate"
            disabled={isRunning || !domainGraph}
            title={t('designer.toolbar.rerunHint')}
            aria-selected={isMedia && expanded === 'generate'}
            className={`designer-node-toolbar__tab${isMedia && expanded === 'generate' ? ' is-active' : ''}`}
            onClick={() => {
              if (isMedia) {
                setExpanded((prev) => (prev === 'generate' ? null : 'generate'));
                return;
              }
              onGenerateNode();
            }}
          >
            {t('designer.toolbar.regenerate')}
          </button>
        ) : null}
        <button
          type="button"
          role="tab"
          aria-selected={expanded === secondaryPanel}
          className={`designer-node-toolbar__tab${expanded === secondaryPanel ? ' is-active' : ''}`}
          data-testid={
            isTextLike ? 'designer-node-toolbar-tab-edit' : 'designer-node-toolbar-tab-upload'
          }
          onClick={() => {
            if (isTextLike) {
              if (hasOutput) {
                startEdit(material?.id || nodeId);
                return;
              }
              setExpanded((prev) => (prev === 'edit' ? null : 'edit'));
              return;
            }
            setExpanded((prev) => (prev === secondaryPanel ? null : secondaryPanel));
          }}
        >
          {isTextLike ? t('designer.toolbar.edit') : t('designer.toolbar.upload')}
        </button>
        {pendingRevision ? (
          <button
            type="button"
            className="designer-node-toolbar__tab"
            data-testid="designer-node-toolbar-tab-compare"
            onClick={() => openRevision(nodeId)}
          >
            {t('designer.revision.compare')}
          </button>
        ) : null}
      </div>

      {expanded === 'generate' && isMedia && canGenerate ? (
        <div
          className="designer-node-toolbar__panel"
          role="tabpanel"
          data-testid="designer-node-toolbar-panel-generate"
        >
          <DesignerMaterialStrip nodeId={nodeId} nodeType={nodeType} />
          <p className="designer-node-toolbar__prompt-hint" data-testid="designer-node-toolbar-prompt-hint">
            {comfyui
              ? t('designer.comfyui.promptHint')
              : t('designer.toolbar.finalPromptHint', {
                  defaultValue:
                    'Shows the last prompt sent to the image/video tool. Edit before regenerate; the leaf agent may still lightly refine locks.',
                })}
          </p>
          <textarea
            className="designer-node-toolbar__prompt nodrag nopan nowheel"
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
              const value = event.target.value;
              setPromptDraft(value);
              patchGenerate({ prompt: value });
            }}
            onWheel={(event) => event.stopPropagation()}
          />
          {comfyui ? <DesignerComfyuiParamsForm nodeId={nodeId} comfyui={comfyui} /> : null}
          <button
            type="button"
            className="designer-node-toolbar__action"
            data-testid="designer-node-toolbar-generate-action"
            disabled={isRunning || !domainGraph}
            title={t('designer.toolbar.rerunHint')}
            onClick={onGenerateNode}
          >
            {t('designer.toolbar.generateAction')}
          </button>
        </div>
      ) : null}

      {expanded === 'edit' ? (
        <div
          className="designer-node-toolbar__panel"
          role="tabpanel"
          data-testid="designer-node-toolbar-panel-edit"
        >
          {material?.textUrl ? (
            <DesignerTextEditor material={material} compact showStartButton={false} startEditKey={1} />
          ) : (
            <p>{t('designer.materials.placeholderHint')}</p>
          )}
        </div>
      ) : null}

      {expanded === 'upload' ? (
        <div
          className={`designer-node-toolbar__panel designer-node-toolbar__panel--upload${dragging ? ' is-dragging' : ''}`}
          role="tabpanel"
          data-testid="designer-node-toolbar-panel-upload"
          onDragEnter={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <input
            ref={fileInputRef}
            type="file"
            className="designer-node-toolbar__file-input"
            accept={
              nodeType === 'audio'
                ? 'audio/*'
                : nodeType === 'video'
                  ? 'video/*'
                  : 'image/*'
            }
            data-testid="designer-node-toolbar-file-input"
            onChange={onFileChange}
          />
          <button
            type="button"
            className="designer-node-toolbar__browse"
            data-testid="designer-node-toolbar-browse"
            onClick={() => fileInputRef.current?.click()}
          >
            {t('designer.toolbar.browseFiles')}
          </button>
          <p className="designer-node-toolbar__drop-hint">{t('designer.toolbar.dropHint')}</p>
          {media.upload?.filename ? (
            <p className="designer-node-toolbar__filename" data-testid="designer-node-toolbar-filename">
              {media.upload.filename}
            </p>
          ) : null}
          <button
            type="button"
            className="designer-node-toolbar__action"
            data-testid="designer-node-toolbar-upload-action"
            disabled={!canConfirmUpload}
            onClick={confirmUpload}
          >
            {t('designer.toolbar.uploadAction')}
          </button>
        </div>
      ) : null}
    </div>
  );
}
