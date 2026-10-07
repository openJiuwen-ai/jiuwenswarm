import {
  Background,
  Controls,
  MiniMap,
  PanOnScrollMode,
  ReactFlow,
  ReactFlowProvider,
  addEdge as appendReactFlowEdge,
  useEdgesState,
  useNodesState,
  useOnSelectionChange,
  useReactFlow,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type OnConnect,
  type OnEdgesChange,
  type OnNodeDrag,
  type OnNodesDelete,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { useTranslation } from 'react-i18next';
import type { DesignerExecutionGraph } from '../executionGraphTypes';
import { resolvedNodeCanvasSize, toReactFlowGraph, type DesignerReactFlowEdge, type DesignerReactFlowNode } from '../designerGraphAdapter';
import { isDesignerPreviewGraph } from '../designerBootstrapGraph';
import { useDesignerStore } from '../designerStore';
import { designerEdgeTypes } from './edges/DesignerEdge';
import { designerNodeTypes } from './nodes/designerNodes';
import { DesignerCanvasDock } from './DesignerCanvasDock';
import { DesignerActivityPeek } from './DesignerActivityPeek';
import { selectLeaderPeek, useDesignerRunStore } from '../designerRunStore';
import {
  DESIGNER_ASSET_DRAG_MIME,
  buildNodeFromLibraryAsset,
  canvasEditGlance,
  offsetCanvasPosition,
} from '../designerCanvasNodes';
import { useDesignerAssetLibraryStore } from '../designerAssetLibraryStore';
import { localPathToFileUri, uploadDesignerAsset } from '../designerAssetUrl';
import { useDesignerUiStore } from '../designerUiStore';
import { DESIGNER_FIT_VIEW_PADDING } from '../designerFitView';

type DesignerCanvasProps = {
  graph: DesignerExecutionGraph;
};

function toPersistableGraph(
  nodes: Node[],
  edges: DesignerReactFlowEdge[],
): { nodes: DesignerReactFlowNode[]; edges: DesignerReactFlowEdge[] } {
  return {
    nodes: nodes.map((node) => ({
      id: node.id,
      type: String(node.type ?? 'text'),
      position: node.position,
      style: node.style as DesignerReactFlowNode['style'],
      data: node.data as DesignerReactFlowNode['data'],
    })),
    edges,
  };
}

/** Sync RF view when graph identity / topology / layout changes — not on config-only edits. */
function buildLayoutSyncKey(graph: DesignerExecutionGraph): string {
  const nodesKey = graph.nodes
    .map((node) => {
      const size = resolvedNodeCanvasSize(node);
      const layout = node.layout ?? {};
      return `${node.id}:${node.type}:${layout.x ?? 0}:${layout.y ?? 0}:${size.width}:${size.height}`;
    })
    .join(';');
  const edgesKey = graph.edges.map((edge) => `${edge.id}:${edge.source}->${edge.target}`).join(';');
  return `${graph.graph_id}|${nodesKey}|${edgesKey}`;
}

function toDesignerEdges(edges: Edge[]): DesignerReactFlowEdge[] {
  return edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: edge.type ?? 'designer',
    label: typeof edge.label === 'string' ? edge.label : undefined,
  }));
}

const CANVAS_GLANCE_HOLD_MS = 2200;
const CANVAS_GLANCE_FADE_MS = 600;

function useFreshCanvasGlance(): { text: string; fading: boolean } {
  const graphId = useDesignerStore((state) => state.domainGraph?.graph_id ?? '');
  const edits = useDesignerStore((state) => state.domainGraph?.metadata?.user_canvas_edits);
  const baseline = useRef<{ graphId: string; count: number } | null>(null);
  const list = Array.isArray(edits) ? edits : [];
  if (!baseline.current || baseline.current.graphId !== graphId) {
    baseline.current = { graphId, count: list.length };
  }
  const { t } = useTranslation();
  const fresh = list.slice(baseline.current.count);
  const op = canvasEditGlance(fresh);
  const text = op ? t(`designer.canvasEdit.${op}`) : '';
  const token = fresh.length > 0 ? `${graphId}:${fresh.length}:${text}` : '';
  const [phase, setPhase] = useState<'show' | 'out' | 'gone'>('gone');
  useEffect(() => {
    if (!token || !text) {
      setPhase('gone');
      return undefined;
    }
    setPhase('show');
    const hold = window.setTimeout(() => setPhase('out'), CANVAS_GLANCE_HOLD_MS);
    const hide = window.setTimeout(
      () => setPhase('gone'),
      CANVAS_GLANCE_HOLD_MS + CANVAS_GLANCE_FADE_MS,
    );
    return () => {
      window.clearTimeout(hold);
      window.clearTimeout(hide);
    };
  }, [token, text]);
  if (phase === 'gone') return { text: '', fading: false };
  return { text, fading: phase === 'out' };
}

function DesignerLeaderStrip() {
  const peek = useDesignerRunStore((state) => selectLeaderPeek(state));
  const bootstrapInProgress = useDesignerStore((state) => state.bootstrapInProgress);
  const glance = useFreshCanvasGlance();
  if (!glance.text && !peek && !bootstrapInProgress) return null;
  return (
    <div
      className={`designer-leader-strip${glance.fading && !peek ? ' is-fading' : ''}`}
      data-testid="designer-leader-strip"
    >
      <span className="designer-leader-strip__label">Leader</span>
      {glance.text ? (
        <strong
          className={`designer-leader-strip__glance${glance.fading ? ' is-fading' : ''}`}
          data-testid="designer-leader-canvas-glance"
        >
          {glance.text}
        </strong>
      ) : null}
      <DesignerActivityPeek
        state={peek}
        variant="leader"
        testId="designer-leader-activity"
      />
    </div>
  );
}

function DesignerCanvasInner({ graph }: DesignerCanvasProps) {
  const layoutSyncKey = useMemo(() => buildLayoutSyncKey(graph), [graph]);
  const graphRef = useRef(graph);
  graphRef.current = graph;
  const reactFlowGraph = useMemo(
    () => toReactFlowGraph(graphRef.current),
    [layoutSyncKey],
  );
  const [nodes, setNodes, onNodesChange] = useNodesState(reactFlowGraph.nodes as Node[]);
  const [edges, setEdges, onEdgesChangeBase] = useEdgesState(reactFlowGraph.edges as Edge[]);
  const { fitView, screenToFlowPosition } = useReactFlow();
  const persistReactFlowLayout = useDesignerStore((state) => state.persistReactFlowLayout);
  const addDomainEdge = useDesignerStore((state) => state.addEdge);
  const addDomainNode = useDesignerStore((state) => state.addNode);
  const removeEdges = useDesignerStore((state) => state.removeEdges);
  const removeNodes = useDesignerStore((state) => state.removeNodes);
  const setSelectedNodeId = useDesignerStore((state) => state.setSelectedNodeId);
  const selectedNodeId = useDesignerStore((state) => state.selectedNodeId);
  const bootstrapInProgress = useDesignerStore((state) => state.bootstrapInProgress);
  const canvasLocked = bootstrapInProgress || isDesignerPreviewGraph(graph);
  const canvasTool = useDesignerUiStore((state) => state.canvasTool);
  const setCanvasTool = useDesignerUiStore((state) => state.setCanvasTool);
  const closeDock = useDesignerUiStore((state) => state.closeDock);
  const getAsset = useDesignerAssetLibraryStore((state) => state.getById);
  const [spacePan, setSpacePan] = useState(false);
  const fittedGraphIdRef = useRef<string | null>(null);
  const handMode = canvasTool === 'hand' || spacePan;
  // Keep visual emphasis separate from edge selection, which controls edge actions.
  const highlightedEdges = useMemo(() => {
    const selectedIds = new Set(nodes.filter((node) => node.selected).map((node) => node.id));
    return edges.map((edge) =>
      selectedIds.has(edge.source) || selectedIds.has(edge.target)
        ? { ...edge, className: 'designer-edge--highlighted' }
        : edge,
    );
  }, [nodes, edges]);

  useEffect(() => {
    const isTypingTarget = (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      return tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code === 'Space' && !event.repeat && !isTypingTarget(event.target)) {
        event.preventDefault();
        setSpacePan(true);
        return;
      }
      if (isTypingTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      if (event.key === 'h' || event.key === 'H') setCanvasTool('hand');
      if (event.key === 'v' || event.key === 'V') setCanvasTool('select');
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code === 'Space') setSpacePan(false);
    };
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
    };
  }, [setCanvasTool]);

  useEffect(() => {
    setNodes((previous) => {
      const selectedIds = new Set(previous.filter((node) => node.selected).map((node) => node.id));
      return reactFlowGraph.nodes.map((node) => ({
        ...(node as Node),
        selected: selectedIds.has(node.id) || node.id === selectedNodeId,
      }));
    });
    setEdges((previous) => {
      const selectedIds = new Set(previous.filter((edge) => edge.selected).map((edge) => edge.id));
      return reactFlowGraph.edges.map((edge) => ({
        ...(edge as Edge),
        selected: selectedIds.has(edge.id),
      }));
    });

    if (fittedGraphIdRef.current !== graph.graph_id) {
      fittedGraphIdRef.current = graph.graph_id;
      requestAnimationFrame(() => {
        void fitView({ padding: DESIGNER_FIT_VIEW_PADDING, duration: 200 });
      });
    }
  }, [graph.graph_id, reactFlowGraph, selectedNodeId, setNodes, setEdges, fitView]);

  useOnSelectionChange({
    onChange: ({ nodes: selectedNodes }) => {
      const only = selectedNodes.length === 1 ? selectedNodes[0] : null;
      setSelectedNodeId(only?.id ?? null);
      const menuId = useDesignerUiStore.getState().successorMenuNodeId;
      if (menuId && only?.id !== menuId) {
        closeDock();
      }
    },
  });

  const onNodeDragStop: OnNodeDrag = useCallback(
    (_event, _node, currentNodes) => {
      persistReactFlowLayout(toPersistableGraph(currentNodes, toDesignerEdges(edges)));
    },
    [edges, persistReactFlowLayout],
  );

  const onConnect: OnConnect = useCallback(
    (connection: Connection) => {
      if (canvasLocked) return;
      // "+" menu sits on the source handle; ignore accidental RF connections
      // while the user is picking a successor type.
      if (useDesignerUiStore.getState().successorMenuNodeId) return;
      if (!connection.source || !connection.target) return;
      const source = connection.source;
      const target = connection.target;
      if (edges.some((edge) => edge.source === source && edge.target === target)) {
        return;
      }
      const id = `e_${source}_${target}_${Date.now().toString(36)}`;
      setEdges((current) =>
        appendReactFlowEdge(
          {
            ...connection,
            id,
            type: 'designer',
          },
          current,
        ),
      );
      addDomainEdge({ id, source, target });
    },
    [addDomainEdge, canvasLocked, edges, setEdges],
  );

  const onEdgesChange: OnEdgesChange = useCallback(
    (changes: EdgeChange[]) => {
      onEdgesChangeBase(changes);
      const removedIds = changes
        .filter((change): change is Extract<EdgeChange, { type: 'remove' }> => change.type === 'remove')
        .map((change) => change.id);
      if (removedIds.length > 0) {
        removeEdges(removedIds);
      }
    },
    [onEdgesChangeBase, removeEdges],
  );

  const onNodesDelete: OnNodesDelete = useCallback(
    (deleted) => {
      removeNodes(deleted.map((node) => node.id));
      closeDock();
    },
    [closeDock, removeNodes],
  );

  const onDragOver = useCallback((event: DragEvent) => {
    if (![...event.dataTransfer.types].includes(DESIGNER_ASSET_DRAG_MIME)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }, []);

  const onDrop = useCallback(
    (event: DragEvent) => {
      if (canvasLocked) return;
      const assetId = event.dataTransfer.getData(DESIGNER_ASSET_DRAG_MIME).trim();
      if (!assetId) return;
      event.preventDefault();
      const asset = getAsset(assetId);
      if (!asset) return;
      const position = offsetCanvasPosition(
        screenToFlowPosition({ x: event.clientX, y: event.clientY }),
        graphRef.current.nodes.length,
      );
      void (async () => {
        try {
          const blob = await fetch(asset.objectUrl).then((response) => response.blob());
          const file = new File([blob], asset.filename, { type: asset.mime_type || blob.type });
          const stored = await uploadDesignerAsset(file);
          const uri = localPathToFileUri(stored.path);
          const node = buildNodeFromLibraryAsset({
            asset,
            existing: graphRef.current.nodes,
            position,
          });
          node.output_ref = {
            kind: node.type,
            uri,
            mime_type: stored.mime_type || asset.mime_type,
            label: stored.filename || asset.filename,
          };
          node.config = {
            ...(node.config ?? {}),
            user_replaced_output: true,
            upload: {
              ...(node.config?.upload ?? {}),
              filename: stored.filename || asset.filename,
              uri,
              mime_type: stored.mime_type || asset.mime_type,
            },
          };
          addDomainNode(node);
          await useDesignerStore.getState().flushSave();
        } catch (error) {
          useDesignerRunStore.setState({
            runError: error instanceof Error ? error.message : String(error),
          });
        }
      })();
    },
    [addDomainNode, canvasLocked, getAsset, screenToFlowPosition],
  );

  return (
    <div className={`designer-canvas-shell${handMode ? ' is-hand' : ''}`}>
      <DesignerLeaderStrip />
      <ReactFlow
        className="designer-page__canvas"
        nodes={nodes}
        edges={highlightedEdges}
        nodeTypes={designerNodeTypes}
        edgeTypes={designerEdgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeDragStop={onNodeDragStop}
        onNodesDelete={onNodesDelete}
        onDragOver={onDragOver}
        onDrop={onDrop}
        onPaneClick={closeDock}
        panOnDrag={handMode || canvasLocked ? true : [1]}
        panOnScroll
        panOnScrollMode={PanOnScrollMode.Free}
        zoomOnScroll={false}
        zoomActivationKeyCode={['Control', 'Meta']}
        selectionOnDrag={!handMode && !canvasLocked}
        nodesDraggable={!handMode && !canvasLocked}
        nodesConnectable={!handMode && !canvasLocked}
        deleteKeyCode={canvasLocked ? null : ['Backspace', 'Delete']}
        elementsSelectable={!canvasLocked}
        fitView
        fitViewOptions={{ padding: DESIGNER_FIT_VIEW_PADDING }}
        minZoom={0.2}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
        data-testid="designer-canvas"
      >
        <Background gap={20} size={1} />
        <Controls position="bottom-right" className="designer-canvas__controls" />
        {canvasLocked ? null : <MiniMap position="bottom-right" pannable zoomable />}
      </ReactFlow>
      {canvasLocked ? null : <DesignerCanvasDock />}
    </div>
  );
}

export function DesignerCanvas({ graph }: DesignerCanvasProps) {
  return (
    <div className="designer-canvas-host">
      <ReactFlowProvider>
        <DesignerCanvasInner key={graph.graph_id} graph={graph} />
      </ReactFlowProvider>
    </div>
  );
}
