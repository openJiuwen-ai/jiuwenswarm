import {
  FileText,
  Headphones,
  Image as ImageIcon,
  Loader2,
  Sheet,
  Trash2,
  Video,
  type LucideIcon,
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Handle, NodeResizeControl, NodeToolbar, Position, type Node, type NodeProps } from '@xyflow/react';
import { isComfyuiNodeConfig } from '../../comfyuiWorkflow';
import { designerAssetPreviewUrl, designerAssetTextUrl } from '../../designerAssetUrl';
import {
  DESIGNER_MATERIAL_SAVED_EVENT,
  preferredDesignerPreviewRef,
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
import { useDesignerRunStore } from '../../designerRunStore';
import { useDesignerStore } from '../../designerStore';
import { useDesignerUiStore } from '../../designerUiStore';
import {
  DESIGNER_DOC_FIT_MIN_BODY,
  DESIGNER_DOC_FIT_MIN_WIDTH,
  DESIGNER_NODE_HEADER_HEIGHT,
  contentAspectFromNodeConfig,
  isDesignerNodeUserResized,
  sizeNodeForContentAspect,
  sizeNodeForDocumentContent,
} from '../../designerCanvasNodes';
import { isDesignerPreviewGraph } from '../../designerBootstrapGraph';
import { isMediaNodeType, supportsNodeToolbar } from '../../mediaNodeConfig';
import { DesignerActivityPeek } from '../DesignerActivityPeek';
import { DesignerNodeToolbar } from '../controls/DesignerNodeToolbar';
import { DesignerNodeSuccessorControl } from './DesignerNodeSuccessorControl';

type DesignerNodeData = DesignerReactFlowNode['data'];
type DesignerFlowNode = Node<DesignerNodeData>;

function modalityIcon(nodeType: string): LucideIcon {
  switch (nodeType) {
    case DESIGNER_NODE_TYPE_TABLE:
      return Sheet;
    case DESIGNER_NODE_TYPE_IMAGE:
      return ImageIcon;
    case DESIGNER_NODE_TYPE_VIDEO:
      return Video;
    case DESIGNER_NODE_TYPE_AUDIO:
      return Headphones;
    case DESIGNER_NODE_TYPE_TEXT:
    default:
      return FileText;
  }
}

function PlaceholderBody({ nodeType }: { nodeType: string }) {
  const Icon = modalityIcon(nodeType);
  return (
    <span className="designer-node__placeholder" data-testid="designer-node-placeholder" data-node-type={nodeType}>
      <Icon className="designer-node__placeholder-icon" size={40} strokeWidth={1.5} aria-hidden />
    </span>
  );
}

function NodeOutputFrame({
  nodeId,
  running,
  children,
}: {
  nodeId: string;
  running: boolean;
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
        <span className="designer-node__running-overlay" data-testid="designer-node-running">
          <Loader2 className="designer-node__running-icon" size={18} aria-hidden />
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
  const handMode = useDesignerUiStore((state) => state.canvasTool === 'hand');
  const onResizeEnd = useCallback(
    (_event: unknown, params: { width: number; height: number }) => {
      updateNodeLayoutSize(nodeId, { width: params.width, height: params.height }, { userResized: true });
    },
    [nodeId, updateNodeLayoutSize],
  );
  if (locked || handMode) return null;
  return (
    <NodeResizeControl
      position="bottom-right"
      minWidth={DESIGNER_DOC_FIT_MIN_WIDTH}
      minHeight={DESIGNER_NODE_HEADER_HEIGHT + DESIGNER_DOC_FIT_MIN_BODY}
      autoScale={false}
      className="designer-node__resize nodrag nopan"
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
  toolbar = null,
}: {
  nodeId: string;
  label: string;
  nodeType: string;
  body: ReactNode;
  media?: boolean;
  mediaFilled?: boolean;
  selected?: boolean;
  toolbar?: ReactNode;
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
  const TypeIcon = modalityIcon(nodeType);
  const showMediaFill = media && (mediaFilled || status === DESIGNER_NODE_STATUS_COMPLETED);
  const removeNodes = useDesignerStore((state) => state.removeNodes);
  const isComfyui = useDesignerStore((state) =>
    isComfyuiNodeConfig(state.domainGraph?.nodes.find((node) => node.id === nodeId)?.config),
  );
  const closeDock = useDesignerUiStore((state) => state.closeDock);

  const onDelete = useCallback(
    (event: MouseEvent) => {
      event.stopPropagation();
      event.preventDefault();
      removeNodes([nodeId]);
      closeDock();
    },
    [closeDock, nodeId, removeNodes],
  );

  return (
    <div
      className={`designer-node${media ? ' designer-node--media' : ''}${showMediaFill ? ' designer-node--media-filled' : ''}${selected ? ' is-selected' : ''}${statusClass} group`}
      data-testid="designer-node"
      data-selected={selected ? 'true' : 'false'}
      data-status={status}
    >
      <Handle type="target" position={Position.Left} className='size-2 bg-gray-500 transition-all ease-out group-hover:size-3' />
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
      <div className="designer-node__header">
        <span className="designer-node__type-icon" aria-hidden data-testid="designer-node-type-icon" data-node-type={nodeType}>
          <TypeIcon size={14} strokeWidth={1.75} />
        </span>
        <span className="designer-node__label">{label}</span>
        <button
          type="button"
          className="designer-node__delete nodrag nopan"
          aria-label={t('designer.nodeActions.delete')}
          title={t('designer.nodeActions.deleteHint')}
          data-testid="designer-node-delete"
          onClick={onDelete}
          onMouseDown={(event) => event.stopPropagation()}
        >
          <Trash2 size={13} strokeWidth={2.25} aria-hidden />
        </button>
      </div>
      <div className="designer-node__body">{body}</div>
      {nodeError ? (
        <p className="designer-node__error" data-testid="designer-node-error" title={nodeError}>
          {nodeError}
        </p>
      ) : null}
      <Handle type="source" position={Position.Right} className='size-2 bg-gray-500 transition-all ease-out group-hover:size-3' />
      <DesignerNodeSuccessorControl nodeId={nodeId} />
      <DesignerNodeResizeHandle nodeId={nodeId} />
      {toolbar}
    </div>
  );
}

function useDesignerAssetText(uri: string | null | undefined): string | null {
  const url = designerAssetTextUrl(uri);
  const [text, setText] = useState<string | null>(null);
  const [reloadAt, setReloadAt] = useState(0);

  useEffect(() => {
    const onSaved = (event: Event) => {
      const savedUri = (event as CustomEvent<{ uri?: string }>).detail?.uri;
      if (savedUri && uri && savedUri === uri) {
        setReloadAt(Date.now());
      }
    };
    window.addEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
    return () => window.removeEventListener(DESIGNER_MATERIAL_SAVED_EVENT, onSaved);
  }, [uri]);

  useEffect(() => {
    if (!url) {
      setText(null);
      return;
    }
    let cancelled = false;
    setText(null);
    const fetchUrl = `${url}${url.includes('?') ? '&' : '?'}t=${reloadAt}`;
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
  }, [url, reloadAt]);

  return text;
}

function useNodePreviewUri(nodeId: string): string | null {
  const outputUri = useDesignerRunStore((state) => {
    const nodeState = state.nodeStates[nodeId];
    return preferredDesignerPreviewRef(
      nodeState?.output_ref,
      nodeState?.candidate_output_ref,
    )?.uri ?? null;
  });
  const domainOutputUri = useDesignerStore(
    (state) => state.domainGraph?.nodes.find((node) => node.id === nodeId)?.output_ref?.uri ?? null,
  );
  return outputUri || domainOutputUri;
}

function DocumentFitBody({
  nodeId,
  contentKey,
  children,
}: {
  nodeId: string;
  contentKey: string;
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
      updateNodeLayoutSize(nodeId, sizeNodeForDocumentContent({ width, height }, 'text'));
    };
    apply();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(apply);
    observer.observe(el);
    return () => observer.disconnect();
  }, [contentKey, nodeId, updateNodeLayoutSize]);

  return (
    <div
      ref={measureRef}
      className="designer-node__fit-content designer-node__fit-content--text"
      data-testid="designer-node-fit-content"
    >
      {children}
    </div>
  );
}

function TextPreviewBody({ nodeId, nodeType }: { nodeId: string; nodeType: string }) {
  const uri = useNodePreviewUri(nodeId);
  const text = useDesignerAssetText(uri);
  if (!text) return <PlaceholderBody nodeType={nodeType} />;
  return (
    <DocumentFitBody nodeId={nodeId} contentKey={text}>
      <p className="designer-node__text-preview" data-testid="designer-node-text-preview">
        {text}
      </p>
    </DocumentFitBody>
  );
}

export function DesignerTextNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');
  const toolbar = (
    <NodeToolbar
      isVisible={selected}
      position={Position.Bottom}
      offset={16}
      align="center"
      className="designer-node-toolbar-portal"
    >
      <DesignerNodeToolbar nodeId={id} nodeType={DESIGNER_NODE_TYPE_TEXT} />
    </NodeToolbar>
  );

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
      toolbar={toolbar}
    />
  );
}

export function DesignerTableNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');
  const toolbar = (
    <NodeToolbar
      isVisible={selected}
      position={Position.Bottom}
      offset={16}
      align="center"
      className="designer-node-toolbar-portal"
    >
      <DesignerNodeToolbar nodeId={id} nodeType={DESIGNER_NODE_TYPE_TABLE} />
    </NodeToolbar>
  );

  return (
    <DesignerNodeShell
      nodeId={id}
      label={nodeData.label}
      nodeType={DESIGNER_NODE_TYPE_TABLE}
      selected={selected}
      body={
        <NodeOutputFrame nodeId={id} running={status === DESIGNER_NODE_STATUS_RUNNING}>
          <TextPreviewBody nodeId={id} nodeType={DESIGNER_NODE_TYPE_TABLE} />
        </NodeOutputFrame>
      }
      toolbar={toolbar}
    />
  );
}

function useFitMediaNodeSize(nodeId: string, nodeType: string, config: Record<string, unknown>) {
  const updateNodeLayoutSize = useDesignerStore((state) => state.updateNodeLayoutSize);
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
  const hinted = contentAspectFromNodeConfig(config);
  useEffect(() => {
    applyRatio(hinted);
  }, [applyRatio, hinted]);
  return applyRatio;
}

export function DesignerMediaNode({ id, data, selected }: NodeProps<DesignerFlowNode>) {
  const nodeData = data as DesignerNodeData;
  const nodeType = nodeData.nodeType;
  const status = useDesignerRunStore((state) => state.nodeStates[id]?.status ?? 'pending');
  const previewUri = useNodePreviewUri(id);
  const previewSrc = designerAssetPreviewUrl(previewUri) || previewUri;
  const hasPreview = Boolean(previewSrc);
  const applyRatio = useFitMediaNodeSize(id, nodeType, nodeData.config || {});

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
      body={
        <NodeOutputFrame nodeId={id} running={status === DESIGNER_NODE_STATUS_RUNNING}>
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
