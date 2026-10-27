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
  type IsValidConnection,
  type Node,
  type OnConnect,
  type OnEdgesChange,
  type OnNodeDrag,
  type OnNodesDelete,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import type { DesignerExecutionGraph, DesignerGraphNode } from '../executionGraphTypes';
import { resolvedNodeCanvasSize, toReactFlowGraph, type DesignerReactFlowEdge, type DesignerReactFlowNode } from '../designerGraphAdapter';
import { isDesignerPreviewGraph } from '../designerBootstrapGraph';
import { isDesignerConnectionAllowed } from '../designerConnectionRules';
import { useDesignerGraphEditingLocked } from '../useDesignerGraphEditingLocked';
import { useDesignerStore } from '../designerStore';
import { designerEdgeTypes } from './edges/DesignerEdge';
import { designerNodeTypes } from './nodes/designerNodes';
import { DesignerCanvasDock } from './DesignerCanvasDock';
import { DesignerRunControl } from './DesignerRunControl';
import { useDesignerRunStore } from '../designerRunStore';
import {
  DESIGNER_ASSET_DRAG_MIME,
  buildNodeFromLibraryAsset,
  offsetCanvasPosition,
} from '../designerCanvasNodes';
import { useDesignerAssetLibraryStore } from '../designerAssetLibraryStore';
import { storeDesignerUpload, uploadOutputRef, uploadedNodeConfig } from '../designerNodeUpload';
import { useDesignerUiStore, type DesignerZoomKind } from '../designerUiStore';
import { isMacPlatform } from './DesignerZoomMenu';
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
      const size = resolvedNodeCanvasSize(node, graph.metadata);
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
  const { fitView, screenToFlowPosition, zoomIn, zoomOut, zoomTo } = useReactFlow();
  const persistReactFlowLayout = useDesignerStore((state) => state.persistReactFlowLayout);
  const addDomainEdge = useDesignerStore((state) => state.addEdge);
  const addDomainNode = useDesignerStore((state) => state.addNode);
  const removeEdges = useDesignerStore((state) => state.removeEdges);
  const removeNodes = useDesignerStore((state) => state.removeNodes);
  const setSelectedNodeId = useDesignerStore((state) => state.setSelectedNodeId);
  const selectedNodeId = useDesignerStore((state) => state.selectedNodeId);
  const bootstrapInProgress = useDesignerStore((state) => state.bootstrapInProgress);
  const canvasLocked = bootstrapInProgress || isDesignerPreviewGraph(graph);
  const editingLocked = useDesignerGraphEditingLocked();
  const canvasTool = useDesignerUiStore((state) => state.canvasTool);
  const setCanvasTool = useDesignerUiStore((state) => state.setCanvasTool);
  const closeDock = useDesignerUiStore((state) => state.closeDock);
  useEffect(() => {
    if (editingLocked) closeDock();
  }, [closeDock, editingLocked]);
  const requestDeleteNodes = useDesignerUiStore((state) => state.requestDeleteNodes);
  const minimapVisible = useDesignerUiStore((state) => state.minimapVisible);
  const layoutRequestId = useDesignerUiStore((state) => state.layoutRequestId);
  const setZoomPercent = useDesignerUiStore((state) => state.setZoomPercent);
  const autoLayout = useDesignerStore((state) => state.autoLayout);
  const getAsset = useDesignerAssetLibraryStore((state) => state.getById);
  const [spacePan, setSpacePan] = useState(false);
  const fittedGraphIdRef = useRef<string | null>(null);
  const lastLayoutRequestRef = useRef(0);
  const handMode = canvasTool === 'hand' || spacePan;
  const edgesVisible = useDesignerUiStore((state) => state.edgesVisible);
  const zoomRequest = useDesignerUiStore((state) => state.zoomRequest);
  const handledZoomRequestRef = useRef(0);
  // Keep visual emphasis separate from edge selection, which controls edge actions.
  const highlightedEdges = useMemo(() => {
    const selectedIds = new Set(nodes.filter((node) => node.selected).map((node) => node.id));
    return edges.map((edge) => {
      const highlighted = selectedIds.has(edge.source) || selectedIds.has(edge.target);
      return {
        ...edge,
        hidden: !edgesVisible,
        ...(highlighted ? { className: 'designer-edge--highlighted' } : {}),
      };
    });
  }, [edgesVisible, nodes, edges]);

  const applyZoom = useCallback(
    (kind: DesignerZoomKind) => {
      if (canvasLocked) return;
      if (kind === 'in') {
        void zoomIn({ duration: 180 });
        return;
      }
      if (kind === 'out') {
        void zoomOut({ duration: 180 });
        return;
      }
      if (kind === 'fit') {
        void fitView({ padding: DESIGNER_FIT_VIEW_PADDING, duration: 200 });
        return;
      }
      void zoomTo(Number(kind) / 100, { duration: 200 });
    },
    [canvasLocked, fitView, zoomIn, zoomOut, zoomTo],
  );

  useEffect(() => {
    if (!zoomRequest || zoomRequest.id === handledZoomRequestRef.current) return;
    handledZoomRequestRef.current = zoomRequest.id;
    applyZoom(zoomRequest.kind);
  }, [applyZoom, zoomRequest]);

  useEffect(() => {
    const isTypingTarget = (target: EventTarget | null) => {
      if (!(target instanceof HTMLElement)) return false;
      const tag = target.tagName;
      return tag === 'INPUT' || tag === 'TEXTAREA' || target.isContentEditable;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (canvasLocked || event.altKey || event.repeat || isTypingTarget(event.target)) return;
      const modifier = isMacPlatform() ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
      if (!modifier) return;
      const zoomInKey = event.code === 'Equal' || event.code === 'NumpadAdd' || event.key === '+' || event.key === '=';
      const zoomOutKey = event.code === 'Minus' || event.code === 'NumpadSubtract' || event.key === '-';
      const fitKey = !event.shiftKey && (event.code === 'Digit0' || event.code === 'Numpad0');
      if (!zoomInKey && !zoomOutKey && !fitKey) return;
      event.preventDefault();
      if (zoomInKey) applyZoom('in');
      else if (zoomOutKey) applyZoom('out');
      else applyZoom('fit');
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [applyZoom, canvasLocked]);

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
    if (!layoutRequestId || layoutRequestId === lastLayoutRequestRef.current) return;
    lastLayoutRequestRef.current = layoutRequestId;
    if (editingLocked) return;
    autoLayout();
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        void fitView({ padding: DESIGNER_FIT_VIEW_PADDING, duration: 220 });
      });
    });
  }, [autoLayout, editingLocked, fitView, layoutRequestId]);

  const syncZoomPercent = useCallback(
    (zoom: number) => {
      setZoomPercent(Math.max(1, Math.round(zoom * 100)));
    },
    [setZoomPercent],
  );

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
      const menu = useDesignerUiStore.getState().nodePortMenu;
      if (menu && only?.id !== menu.nodeId) {
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

  // Refuse unsupported wires while the user is still dragging (issue #5382).
  const isValidConnection: IsValidConnection = useCallback(
    (connection: Connection | Edge) => {
      if (canvasLocked || editingLocked) return false;
      const source = connection.source;
      const target = connection.target;
      if (!source || !target || source === target) return false;
      const typeById = new Map(
        graphRef.current.nodes.map((node) => [String(node.id), String(node.type ?? '')]),
      );
      return isDesignerConnectionAllowed(typeById.get(source), typeById.get(target));
    },
    [canvasLocked, editingLocked],
  );

  const onConnect: OnConnect = useCallback(
    (connection: Connection) => {
      if (canvasLocked || editingLocked) return;
      if (!connection.source || !connection.target) return;
      const source = connection.source;
      const target = connection.target;
      if (source === target) return;
      // Same rule as `isValidConnection`: never persist an unsupported wire.
      const typeById = new Map(
        graphRef.current.nodes.map((node) => [String(node.id), String(node.type ?? '')]),
      );
      if (!isDesignerConnectionAllowed(typeById.get(source), typeById.get(target))) return;
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
    [addDomainEdge, canvasLocked, editingLocked, edges, setEdges],
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

  const onBeforeDelete = useCallback(
    async ({ nodes: deleting }: { nodes: Node[] }) => {
      if (deleting.length === 0) return true;
      if (canvasLocked || editingLocked) return false;
      requestDeleteNodes(deleting.map((node) => node.id));
      return false;
    },
    [canvasLocked, editingLocked, requestDeleteNodes],
  );

  const onDragOver = useCallback((event: DragEvent) => {
    if (![...event.dataTransfer.types].includes(DESIGNER_ASSET_DRAG_MIME)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
  }, []);

  const onDrop = useCallback(
    (event: DragEvent) => {
      if (canvasLocked || editingLocked) return;
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
          const stored = await storeDesignerUpload(file);
          const node = buildNodeFromLibraryAsset({
            asset,
            existing: graphRef.current.nodes,
            position,
          });
          node.output_ref = uploadOutputRef(node.type, stored);
          node.config = uploadedNodeConfig(
            node.config as Record<string, unknown>,
            stored,
            asset.id,
          ) as DesignerGraphNode['config'];
          addDomainNode(node);
          await useDesignerStore.getState().flushSave();
        } catch (error) {
          useDesignerRunStore.setState({
            runError: error instanceof Error ? error.message : String(error),
          });
        }
      })();
    },
    [addDomainNode, canvasLocked, editingLocked, getAsset, screenToFlowPosition],
  );

  return (
    <div className={`designer-canvas-shell${handMode ? ' is-hand' : ''}${edgesVisible ? '' : ' is-edges-hidden'}${editingLocked ? ' is-editing-locked' : ''}`}>
      <ReactFlow
        className="designer-page__canvas"
        nodes={nodes}
        edges={highlightedEdges}
        nodeTypes={designerNodeTypes}
        edgeTypes={designerEdgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onNodeDragStop={onNodeDragStop}
        onNodesDelete={onNodesDelete}
        onBeforeDelete={onBeforeDelete}
        onDragOver={onDragOver}
        onDrop={onDrop}
        onPaneClick={closeDock}
        onMove={(_, viewport) => syncZoomPercent(viewport.zoom)}
        onInit={(instance) => syncZoomPercent(instance.getZoom())}
        panOnDrag={handMode || canvasLocked ? true : [1]}
        panOnScroll
        panOnScrollMode={PanOnScrollMode.Free}
        zoomOnScroll={false}
        zoomActivationKeyCode={['Control', 'Meta']}
        selectionOnDrag={!handMode && !canvasLocked}
        nodesDraggable={!handMode && !canvasLocked && !editingLocked}
        // Port handles stay connectable in select and hand tools; only lock blocks them.
        nodesConnectable={!canvasLocked && !editingLocked}
        connectionDragThreshold={8}
        connectOnClick={false}
        deleteKeyCode={canvasLocked || editingLocked ? null : ['Backspace', 'Delete']}
        elementsSelectable={!canvasLocked}
        fitView
        fitViewOptions={{ padding: DESIGNER_FIT_VIEW_PADDING }}
        minZoom={0.2}
        maxZoom={2}
        proOptions={{ hideAttribution: true }}
        data-testid="designer-canvas"
      >
        <Background gap={20} size={1} />
        {canvasLocked || !minimapVisible ? null : (
          <MiniMap
            position="bottom-right"
            pannable
            zoomable
            className="designer-canvas__minimap"
          />
        )}
        <Controls
          position="bottom-right"
          orientation="horizontal"
          className={`designer-canvas__controls${minimapVisible && !canvasLocked ? ' designer-canvas__controls--below-minimap' : ''}`}
          showInteractive={false}
        />
        {import.meta.env.DEV ? (
          <DesignerRunControl graph={graph} disabled={canvasLocked} />
        ) : null}
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
