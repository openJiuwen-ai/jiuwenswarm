import { Loader2, Sheet } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { NodeResizeControl, NodeToolbar, Position, type Node, type NodeProps } from '@xyflow/react';
import { isComfyuiNodeConfig } from '../../comfyuiWorkflow';
import { designerAssetPreviewUrl, designerAssetTextUrl } from '../../designerAssetUrl';
import {
  DESIGNER_MATERIAL_SAVED_EVENT,
  resolveDesignerNodePreviewRef,
} from '../../designerMaterials';
import {
  DESIGNER_NODE_STATUS_COMPLETED,
  DESIGNER_NODE_STATUS_FAILED,
  DESIGNER_NODE_STATUS_RUNNING,
  DESIGNER_NODE_TYPE_AUDIO,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_VIDEO,
} from '../../executionGraphTypes';
import type { DesignerReactFlowNode } from '../../designerGraphAdapter';
import { DesignerModalityTypeIcon } from '../../designerIcons';
import { useDesignerRunStore } from '../../designerRunStore';
import { isDesignerModerationError } from '../../designerRunView';
import { useDesignerGraphEditingLocked } from '../../useDesignerGraphEditingLocked';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';
import {
  DESIGNER_DOC_FIT_MIN_BODY,
  DESIGNER_DOC_FIT_MIN_WIDTH,
  DESIGNER_NODE_HEADER_HEIGHT,
  contentAspectForNode,
  isDesignerNodeUserResized,
  sizeNodeForContentAspect,
  sizeNodeForDocumentContent,
} from '../../designerCanvasNodes';
import { isDesignerPreviewGraph } from '../../designerBootstrapGraph';
import { isMediaNodeType, supportsNodeToolbar } from '../../mediaNodeConfig';
import { MarkdownRenderer } from '../../../../components/MarkdownRenderer';
import { DesignerStoryboardTable } from '../../DesignerStoryboardTable';
import { DesignerActivityPeek } from '../DesignerActivityPeek';
import { DesignerNodeActionBar } from '../controls/DesignerNodeActionBar';
import { DesignerNodeToolbar } from '../controls/DesignerNodeToolbar';
import { DesignerNodePortControl } from './DesignerNodePortControl';

type DesignerNodeData = DesignerReactFlowNode['data'];
type DesignerFlowNode = Node<DesignerNodeData>;

function PlaceholderBody({ nodeType }: { nodeType: string }) {
  return (
    <span className="designer-node__placeholder" data-testid="designer-node-placeholder" data-node-type={nodeType}>
      {nodeType === DESIGNER_NODE_TYPE_TABLE ? (
        <Sheet className="designer-node__placeholder-icon" size={40} strokeWidth={1.5} aria-hidden />
      ) : (
        <DesignerModalityTypeIcon nodeType={nodeType} className="designer-node__placeholder-icon" />
      )}
    </span>
  );
}

function NodeOutputFrame({
  nodeId,
  running,
  shimmer = false,
  children,
}: {
  nodeId: string;
  running: boolean;
  shimmer?: boolean;
  children: ReactNode;
}) {
  const activity = useDesignerRunStore((state) => {
    const nodeState = state.nodeStates[nodeId];
    if (!nodeState) return null;
    return {
      activity: nodeState.activity,
      activity_tail: nodeState.activity_tail,
      activity_log: nodeState.activity_log,
    };
  });
  return (
    <span className="designer-node__output-frame">
      {children}
      {running ? (
        <span
          className={`designer-node__running-overlay${shimmer ? ' designer-node__running-overlay--shimmer' : ''}`}
          data-testid="designer-node-running"
          data-variant={shimmer ? 'shimmer' : 'spinner'}
        >
          {shimmer ? null : <Loader2 className="designer-node__running-icon" size={18} aria-hidden />}
          <DesignerActivityPeek state={activity} testId="designer-node-activity" />
        </span>
      ) : null}
    </span>
  );
}

function DesignerNodeResizeHandle({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation();
  const updateNodeLayoutSize = useDesignerStore((state) => state.updateNodeLayoutSize);
  const locked = useDesignerStore(
    (state) => state.bootstrapInProgress || isDesignerPreviewGraph(state.domainGraph),
  );
  const editingLocked = useDesignerGraphEditingLocked();
  const handMode = useDesignerUiStore((state) => state.canvasTool === 'hand');
  const onResizeEnd = useCallback(
    (_event: unknown, params: { width: number; height: number }) => {
      if (editingLocked) return;
      updateNodeLayoutSize(nodeId, { width: params.width, height: params.height }, { userResized: true });
    },
    [editingLocked, nodeId, updateNodeLayoutSize],
  );
  if (locked || handMode) return null;
  return (
    <NodeResizeControl
      position="bottom-right"
      minWidth={DESIGNER_DOC_FIT_MIN_WIDTH}
      minHeight={DESIGNER_NODE_HEADER_HEIGHT + DESIGNER_DOC_FIT_MIN_BODY}
      autoScale={false}
      shouldResize={() => !editingLocked}
      className={`designer-node__resize nodrag nopan${editingLocked ? ' designer-edit-control' : ''}`}
      onResizeEnd={onResizeEnd}
    >
      <span
        className="designer-node__resize-grip"
        aria-label={t('designer.nodeActions.resize')}
        title={t('designer.nodeActions.resizeHint')}
        data-testid="designer-node-resize"
      />
    </NodeResizeControl>
  );
}

function DesignerNodeShell({
  nodeId,
  label,
  nodeType,
  body,
  media = false,
  mediaFilled = false,
  selected = false,
  resolutionLabel = null,
  toolbar = null,
  generatingShimmer = false,
}: {
  nodeId: string;
  label: string;
  nodeType: string;
  body: ReactNode;
  media?: boolean;
  mediaFilled?: boolean;
  selected?: boolean;
  /** Pixel size label for image/video content, e.g. "1920×1080". */
  resolutionLabel?: string | null;
  toolbar?: ReactNode;
  /** Image and video generation replaces the spinner with a scrolling fill. */
  generatingShimmer?: boolean;
}) {
  const { t } = useTranslation();
  const status = useDesignerRunStore(
    (state) => state.nodeStates[nodeId]?.status ?? 'pending',
  );
  const nodeError = useDesignerRunStore((state) => {
    const nodeState = state.nodeStates[nodeId];
    if (nodeState?.status !== DESIGNER_NODE_STATUS_FAILED) return '';
    return String(nodeState.error || '').trim();
  });
  const statusClass =
    status === DESIGNER_NODE_STATUS_RUNNING
      ? ' is-running'
      : status === DESIGNER_NODE_STATUS_COMPLETED
        ? ' is-completed'
        : status === DESIGNER_NODE_STATUS_FAILED
          ? ' is-failed'
          : '';
  const showMediaFill = media && (mediaFilled || status === DESIGNER_NODE_STATUS_COMPLETED);
  const isComfyui = useDesignerStore((state) =>
    isComfyuiNodeConfig(state.domainGraph?.nodes.find((node) => node.id === nodeId)?.config),
  );

  return (
    <div
      className={`designer-node${media ? ' designer-node--media' : ''}${showMediaFill ? ' designer-node--media-filled' : ''}${generatingShimmer ? ' is-generating-shimmer' : ''}${selected ? ' is-selected' : ''}${statusClass} group`}
      data-testid="designer-node"
      data-selected={selected ? 'true' : 'false'}
      data-status={status}
    >
      <NodeToolbar
        isVisible={selected}
        position={Position.Top}
        offset={12}
        align="center"
        className="designer-node-action-bar-portal"
      >
        <DesignerNodeActionBar nodeId={nodeId} nodeType={nodeType} />
      </NodeToolbar>
      <div className="designer-node__caption">
        <span className="designer-node__type-icon" aria-hidden data-testid="designer-node-type-icon" data-node-type={nodeType}>
          {nodeType === DESIGNER_NODE_TYPE_TABLE ? (
            <Sheet size={12} strokeWidth={1.75} />
          ) : (
            <DesignerModalityTypeIcon nodeType={nodeType} />
          )}
        </span>
        <span className="designer-node__label">{label}</span>
        {resolutionLabel ? (
          <span className="designer-node__resolution" data-testid="designer-node-resolution">
            {resolutionLabel}
          </span>
        ) : null}
      </div>
      <div className="designer-node__card">
        <DesignerNodePortControl nodeId={nodeId} side="predecessor" />
        {isComfyui ? (
          <span
            className="designer-node__comfyui-badge"
            title={t('designer.comfyui.badgeHint')}
            aria-label={t('designer.comfyui.badgeHint')}
            data-testid="designer-node-comfyui-badge"
          >
            C
          </span>
        ) : null}
        <div className="designer-node__body">{body}</div>
        {nodeError ? (
          <p className="designer-node__error nodrag nopan nowheel" data-testid="designer-node-error" title={nodeError}>
            {isDesignerModerationError(nodeError) ? t('designer.run.moderationBlocked') : nodeError}
          </p>
        ) : null}
        <DesignerNodePortControl nodeId={nodeId} side="successor" />
        <DesignerNodeResizeHandle nodeId={nodeId} />
      </div>
      {toolbar}
    </div>
  );
}

/** `producedAt` changes when a rerun overwrites the output in place under the same uri. */
function useDesignerAssetText(uri: string | null | undefined, producedAt: number): string | null {
  const url = designerAssetTextUrl(uri);
  const [text, setText] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState(0);
  const reloadKey = `${producedAt}-${savedAt}`;
  const loadedUrl = useRef<string | null>(null);

  useEffect(() => {
    const onSaved = (event: Event) => {
      const savedUri = (event as CustomEvent<{ uri?: string }>).detail?.uri;
      if (savedUri && uri && savedUri === uri) {
        setSavedAt(Date.now());
      }
    };
    window.addEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
    return () => window.removeEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
  }, [uri]);

  useEffect(() => {
    if (!url) {
      loadedUrl.current = null;
      setText(null);
      return;
    }
    let cancelled = false;
    // Keep the current text on screen while the same file reloads.
    if (loadedUrl.current !== url) setText(null);
    loadedUrl.current = url;
    const fetchUrl = `${url}${url.includes('?') ? '&' : '?'}t=${reloadKey}`;
    void fetch(fetchUrl)
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.text();
      })
      .then((value) => {
        if (!cancelled) setText(value);
      })
      .catch(() => {
        if (!cancelled) setText(null);
      });
    return () => {
      cancelled = true;
    };
  }, [url, reloadKey]);

  return text;
}

function useNodePreviewUri(nodeId: string): string | null {
  const node = useDesignerStore(
    (state) => state.domainGraph?.nodes.find((item) => item.id === nodeId) ?? null,
  );
  const nodeState = useDesignerRunStore((state) => state.nodeStates[nodeId]);
  return resolveDesignerNodePreviewRef(node, nodeState)?.uri ?? null;
}

function useNodeProducedAt(nodeId: string): number {
  return useDesignerRunStore((state) => state.nodeStates[nodeId]?.completed_at ?? 0);
}

function DocumentFitBody({
  nodeId,
  contentKey,
  kind = 'text',
  children,
}: {
  nodeId: string;
  contentKey: string;
  kind?: 'text' | 'table';
  children: ReactNode;
}) {
  const measureRef = useRef<HTMLDivElement>(null);
  const updateNodeLayoutSize = useDesignerStore((state) => state.updateNodeLayoutSize);

  useLayoutEffect(() => {
    const el = measureRef.current;
    if (!el) return;
    const apply = () => {
      const node = useDesignerStore.getState().domainGraph?.nodes.find((item) => item.id === nodeId);
      if (isDesignerNodeUserResized(node?.config as Record<string, unknown> | undefined)) return;
      const width = Math.max(el.scrollWidth, el.offsetWidth);
      const height = Math.max(el.scrollHeight, el.offsetHeight);
      if (width < 8 || height < 8) return;
      updateNodeLayoutSize(nodeId, sizeNodeForDocumentContent({ width, height }, kind));
    };
    apply();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(apply);
    observer.observe(el);
    return () => observer.disconnect();
  }, [contentKey, kind, nodeId, updateNodeLayoutSize]);

  return (
    <div
      ref={measureRef}
      className={`designer-node__fit-content designer-node__fit-content--${kind}`}
      data-testid="designer-node-fit-content"
      data-variant={kind}
    >
      {children}
    </div>
  );
}

function TablePreviewBody({ nodeId }: { nodeId: string }) {
  const uri = useNodePreviewUri(nodeId);
  const text = useDesignerAssetText(uri, useNodeProducedAt(nodeId));
  if (!text) return <PlaceholderBody nodeType={DESIGNER_NODE_TYPE_TABLE} />;
  return (
    <DocumentFitBody nodeId={nodeId} contentKey={text} kind="table">
      <DesignerStoryboardTable text={text} testId="designer-node-storyboard-table" layout="node" />
    </DocumentFitBody>
  );
}

function TextPreviewBody({ nodeId, nodeType }: { nodeId: string; nodeType: string }) {
  const uri = useNodePreviewUri(nodeId);
  const text = useDesignerAssetText(uri, useNodeProducedAt(nodeId));
  if (!text) return <PlaceholderBody nodeType={nodeType} />;
  return (
    <DocumentFitBody nodeId={nodeId} contentKey={text}>
      <MarkdownRenderer
        content={text}
        className="designer-node__text-preview chat-markdown"
        testId="designer-node-text-preview"
      />
    </DocumentFitBody>
  );
}

export function DesignerTextNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');

  return (
    <DesignerNodeShell
      nodeId={id}
      label={nodeData.label}
      nodeType={DESIGNER_NODE_TYPE_TEXT}
      selected={selected}
      body={
        <NodeOutputFrame nodeId={id} running={status === DESIGNER_NODE_STATUS_RUNNING}>
          <TextPreviewBody nodeId={id} nodeType={DESIGNER_NODE_TYPE_TEXT} />
        </NodeOutputFrame>
      }
    />
  );
}

export function DesignerTableNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');

  return (
    <DesignerNodeShell
      nodeId={id}
      label={nodeData.label}
      nodeType={DESIGNER_NODE_TYPE_TABLE}
      selected={selected}
      body={
        <NodeOutputFrame nodeId={id} running={status === DESIGNER_NODE_STATUS_RUNNING}>
          <TablePreviewBody nodeId={id} />
        </NodeOutputFrame>
      }
    />
  );
}

function useFitMediaNodeSize(
  nodeId: string,
  nodeType: string,
  config: Record<string, unknown>,
  hasPreview: boolean,
) {
  const updateNodeLayoutSize = useDesignerStore((state) => state.updateNodeLayoutSize);
  const hinted = useDesignerStore((state) => contentAspectForNode(nodeType, config, state.domainGraph?.metadata));
  const applyRatio = useCallback(
    (ratio: number | null | undefined) => {
      if (nodeType !== DESIGNER_NODE_TYPE_IMAGE && nodeType !== DESIGNER_NODE_TYPE_VIDEO) {
        return;
      }
      if (isDesignerNodeUserResized(config)) return;
      if (!ratio || !Number.isFinite(ratio) || ratio <= 0) return;
      updateNodeLayoutSize(nodeId, sizeNodeForContentAspect(ratio));
    },
    [config, nodeId, nodeType, updateNodeLayoutSize],
  );
  // Before there is output, frame the node at the ratio it will generate; afterwards the media's own size wins.
  useEffect(() => {
    if (!hasPreview) applyRatio(hinted);
  }, [applyRatio, hasPreview, hinted]);
  return applyRatio;
}

export function DesignerMediaNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const nodeType = nodeData.nodeType;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');
  const previewUri = useNodePreviewUri(id);
  const previewSrc = designerAssetPreviewUrl(previewUri) || previewUri;
  const hasPreview = Boolean(previewSrc);
  const applyRatio = useFitMediaNodeSize(id, nodeType, nodeData.config || {}, hasPreview);
  const [resolutionLabel, setResolutionLabel] = useState<string | null>(null);

  useEffect(() => {
    if (!hasPreview || (nodeType !== DESIGNER_NODE_TYPE_IMAGE && nodeType !== DESIGNER_NODE_TYPE_VIDEO)) {
      setResolutionLabel(null);
    }
  }, [hasPreview, nodeType, previewSrc]);

  let inner: ReactNode = <PlaceholderBody nodeType={nodeType} />;
  if (hasPreview && nodeType === DESIGNER_NODE_TYPE_IMAGE) {
    inner = (
      <img
        className="designer-node__media-preview"
        src={previewSrc || ''}
        alt={nodeData.label}
        data-testid="designer-node-image-preview"
        onLoad={(event) => {
          const image = event.currentTarget;
          if (image.naturalWidth > 0 && image.naturalHeight > 0) {
            applyRatio(image.naturalWidth / image.naturalHeight);
            setResolutionLabel(`${image.naturalWidth}×${image.naturalHeight}`);
          }
        }}
      />
    );
  } else if (hasPreview && nodeType === DESIGNER_NODE_TYPE_VIDEO) {
    inner = (
      <video
        className="designer-node__media-preview"
        src={previewSrc || ''}
        playsInline
        controls={true}
        autoPlay={false}
        data-testid="designer-node-video-preview"
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          if (video.videoWidth > 0 && video.videoHeight > 0) {
            applyRatio(video.videoWidth / video.videoHeight);
            setResolutionLabel(`${video.videoWidth}×${video.videoHeight}`);
          }
        }}
      />
    );
  } else if (hasPreview && nodeType === DESIGNER_NODE_TYPE_AUDIO) {
    inner = (
      <audio
        className="designer-node__audio-preview"
        src={previewSrc || ''}
        controls
        data-testid="designer-node-uploaded-audio"
      />
    );
  }

  const generatingShimmer =
    status === DESIGNER_NODE_STATUS_RUNNING
    && (
      nodeType === DESIGNER_NODE_TYPE_IMAGE
      || nodeType === DESIGNER_NODE_TYPE_VIDEO
      || nodeType === DESIGNER_NODE_TYPE_AUDIO
    );

  const toolbar = supportsNodeToolbar(nodeType) ? (
    <NodeToolbar
      isVisible={selected}
      position={Position.Bottom}
      offset={16}
      align="center"
      className="designer-node-toolbar-portal"
    >
      <DesignerNodeToolbar nodeId={id} nodeType={nodeType} />
    </NodeToolbar>
  ) : null;

  return (
    <DesignerNodeShell
      nodeId={id}
      label={nodeData.label}
      nodeType={nodeType}
      media={isMediaNodeType(nodeType)}
      mediaFilled={hasPreview}
      selected={selected}
      resolutionLabel={
        hasPreview && (nodeType === DESIGNER_NODE_TYPE_IMAGE || nodeType === DESIGNER_NODE_TYPE_VIDEO)
          ? resolutionLabel
          : null
      }
      generatingShimmer={generatingShimmer}
      body={
        <NodeOutputFrame
          nodeId={id}
          running={status === DESIGNER_NODE_STATUS_RUNNING}
          shimmer={generatingShimmer}
        >
          {inner}
        </NodeOutputFrame>
      }
      toolbar={toolbar}
    />
  );
}

export const designerNodeTypes = {
  [DESIGNER_NODE_TYPE_TEXT]: DesignerTextNode,
  [DESIGNER_NODE_TYPE_TABLE]: DesignerTableNode,
  [DESIGNER_NODE_TYPE_IMAGE]: DesignerMediaNode,
  [DESIGNER_NODE_TYPE_VIDEO]: DesignerMediaNode,
  [DESIGNER_NODE_TYPE_AUDIO]: DesignerMediaNode,
};
