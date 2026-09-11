import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from 'react';
import {
  COMPONENT_CENTER_ATTRACTION_STRENGTH,
  computeRelationshipGraphComponents,
  seedRelationshipGraphPositions,
  stepRelationshipGraphLayout,
  type RelationshipGraphComponent,
  type RelationshipLayoutNode,
} from './relationshipGraphLayout';

export type RelationshipGraphNode = {
  id: string;
  label: string;
  kind: string;
  reference?: boolean;
};

export type RelationshipGraphEdge = {
  id: string;
  source: string;
  target: string;
  type: string;
  confidence: number;
  directed?: boolean;
};

export type InteractiveRelationshipGraphHandle = {
  fitView: () => void;
  focusNode: (nodeId: string) => void;
  zoomIn: () => void;
  zoomOut: () => void;
};

type PositionedNode = RelationshipGraphNode & RelationshipLayoutNode & { degree: number };
type Transform = { x: number; y: number; scale: number };
type PointerInteraction =
  | { kind: 'pan'; pointerId: number; clientX: number; clientY: number; originX: number; originY: number; moved: boolean }
  | { kind: 'node'; pointerId: number; nodeId: string; moved: boolean };

type CanvasPalette = {
  primary: string;
  primarySubtle: string;
  surface: string;
  text: string;
  textSecondary: string;
  textTertiary: string;
  border: string;
  borderStrong: string;
  warning: string;
};

type InteractiveRelationshipGraphProps = {
  nodes: RelationshipGraphNode[];
  edges: RelationshipGraphEdge[];
  selectedNodeId?: string;
  selectedEdgeId?: string;
  ariaLabel: string;
  onSelectNode?: (nodeId: string | null) => void;
  onSelectEdge?: (edgeId: string | null) => void;
};

const MIN_SCALE = 0.18;
const MAX_SCALE = 2.4;

function clampScale(scale: number): number {
  return Math.max(MIN_SCALE, Math.min(MAX_SCALE, scale));
}

function nodeRadius(node: PositionedNode): number {
  const base = node.kind === 'team' ? 10 : 8;
  return Math.min(24, base + Math.sqrt(Math.max(0, node.degree)) * 2.2);
}

function truncate(value: string, limit: number): string {
  return value.length > limit ? `${value.slice(0, limit - 1)}…` : value;
}

function pointToSegmentDistance(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const lengthSquared = dx * dx + dy * dy;
  if (lengthSquared === 0) return Math.hypot(px - ax, py - ay);
  const ratio = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSquared));
  return Math.hypot(px - (ax + ratio * dx), py - (ay + ratio * dy));
}

function readPalette(canvas: HTMLCanvasElement): CanvasPalette {
  const rootStyle = window.getComputedStyle(document.documentElement);
  const canvasStyle = window.getComputedStyle(canvas);
  const token = (name: string, fallback: string) => rootStyle.getPropertyValue(name).trim() || fallback;
  const text = canvasStyle.color || 'currentColor';
  return {
    primary: token('--color-action-primary', text),
    primarySubtle: token('--color-action-primary-subtle', canvasStyle.backgroundColor),
    surface: token('--color-surface-card', canvasStyle.backgroundColor),
    text: token('--color-text-primary', text),
    textSecondary: token('--color-text-secondary', text),
    textTertiary: token('--color-text-tertiary', text),
    border: token('--color-border-default', text),
    borderStrong: token('--color-border-strong', text),
    warning: token('--color-feedback-warning', text),
  };
}

export const InteractiveRelationshipGraph = forwardRef<InteractiveRelationshipGraphHandle, InteractiveRelationshipGraphProps>(
  function InteractiveRelationshipGraph({ nodes, edges, selectedNodeId = '', selectedEdgeId = '', ariaLabel, onSelectNode, onSelectEdge }, forwardedRef) {
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const nodesRef = useRef<PositionedNode[]>([]);
    const edgesRef = useRef<RelationshipGraphEdge[]>([]);
    const componentsRef = useRef<RelationshipGraphComponent[]>([]);
    const selectedNodeIdRef = useRef(selectedNodeId);
    const selectedEdgeIdRef = useRef(selectedEdgeId);
    const hoveredNodeIdRef = useRef('');
    const pointerRef = useRef<PointerInteraction | null>(null);
    const transformRef = useRef<Transform>({ x: 0, y: 0, scale: 1 });
    const paletteRef = useRef<CanvasPalette | null>(null);
    const layoutTicksRemainingRef = useRef(0);

    const fitView = useCallback(() => {
      const canvas = canvasRef.current;
      const positionedNodes = nodesRef.current;
      if (!canvas || positionedNodes.length === 0) return;
      const rect = canvas.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return;
      const minX = Math.min(...positionedNodes.map(node => node.x - nodeRadius(node)));
      const maxX = Math.max(...positionedNodes.map(node => node.x + nodeRadius(node)));
      const minY = Math.min(...positionedNodes.map(node => node.y - nodeRadius(node)));
      const maxY = Math.max(...positionedNodes.map(node => node.y + nodeRadius(node) + 22));
      const graphWidth = Math.max(1, maxX - minX);
      const graphHeight = Math.max(1, maxY - minY);
      const paddingX = Math.min(120, rect.width * 0.22);
      const paddingY = Math.min(100, rect.height * 0.22);
      const scale = clampScale(Math.min((rect.width - paddingX) / graphWidth, (rect.height - paddingY) / graphHeight));
      transformRef.current = {
        x: rect.width / 2 - ((minX + maxX) / 2) * scale,
        y: rect.height / 2 - ((minY + maxY) / 2) * scale,
        scale,
      };
    }, []);

    const zoomAt = useCallback((factor: number, screenX?: number, screenY?: number) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const current = transformRef.current;
      const anchorX = screenX ?? rect.width / 2;
      const anchorY = screenY ?? rect.height / 2;
      const scale = clampScale(current.scale * factor);
      const worldX = (anchorX - current.x) / current.scale;
      const worldY = (anchorY - current.y) / current.scale;
      transformRef.current = {
        x: anchorX - worldX * scale,
        y: anchorY - worldY * scale,
        scale,
      };
    }, []);

    const focusNode = useCallback((nodeId: string) => {
      const canvas = canvasRef.current;
      const node = nodesRef.current.find(item => item.id === nodeId);
      if (!canvas || !node) return;
      const rect = canvas.getBoundingClientRect();
      const scale = Math.max(0.9, transformRef.current.scale);
      transformRef.current = {
        x: rect.width / 2 - node.x * scale,
        y: rect.height / 2 - node.y * scale,
        scale,
      };
    }, []);

    useImperativeHandle(forwardedRef, () => ({ fitView, focusNode, zoomIn: () => zoomAt(1.2), zoomOut: () => zoomAt(1 / 1.2) }), [fitView, focusNode, zoomAt]);

    useEffect(() => {
      selectedNodeIdRef.current = selectedNodeId;
      if (selectedNodeId) focusNode(selectedNodeId);
    }, [focusNode, selectedNodeId]);

    useEffect(() => {
      selectedEdgeIdRef.current = selectedEdgeId;
    }, [selectedEdgeId]);

    useEffect(() => {
      const previousById = new Map(nodesRef.current.map(node => [node.id, node]));
      const degreeById = new Map(nodes.map(node => [node.id, 0]));
      edges.forEach(edge => {
        degreeById.set(edge.source, (degreeById.get(edge.source) || 0) + 1);
        degreeById.set(edge.target, (degreeById.get(edge.target) || 0) + 1);
      });
      const positioned = nodes.map<PositionedNode>(node => ({
        ...node,
        x: previousById.get(node.id)?.x ?? 0,
        y: previousById.get(node.id)?.y ?? 0,
        vx: 0,
        vy: 0,
        degree: degreeById.get(node.id) || 0,
      }));
      const unseeded = positioned.filter(node => !previousById.has(node.id));
      if (unseeded.length > 0) {
        const seeded = positioned.map(node => ({ ...node }));
        seedRelationshipGraphPositions(seeded, 900, 620);
        const seededById = new Map(seeded.map(node => [node.id, node]));
        unseeded.forEach(node => {
          const seed = seededById.get(node.id);
          if (!seed) return;
          node.x = seed.x;
          node.y = seed.y;
        });
      }
      nodesRef.current = positioned;
      edgesRef.current = edges;
      componentsRef.current = computeRelationshipGraphComponents(positioned, edges);

      const settleSteps = Math.min(260, Math.max(90, Math.floor(12_000 / Math.max(1, positioned.length))));
      for (let step = 0; step < settleSteps; step += 1) {
        stepRelationshipGraphLayout(positioned, edges, 900, 620, componentsRef.current, COMPONENT_CENTER_ATTRACTION_STRENGTH);
      }
      layoutTicksRemainingRef.current = 120;
      const frame = window.requestAnimationFrame(fitView);
      return () => window.cancelAnimationFrame(frame);
    }, [edges, fitView, nodes]);

    useEffect(() => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const resize = () => {
        const rect = canvas.getBoundingClientRect();
        const pixelRatio = window.devicePixelRatio || 1;
        canvas.width = Math.max(1, Math.floor(rect.width * pixelRatio));
        canvas.height = Math.max(1, Math.floor(rect.height * pixelRatio));
        paletteRef.current = readPalette(canvas);
      };
      resize();
      const resizeObserver = new ResizeObserver(resize);
      resizeObserver.observe(canvas);
      const themeObserver = new MutationObserver(() => {
        paletteRef.current = readPalette(canvas);
      });
      themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class', 'data-theme'] });
      return () => {
        resizeObserver.disconnect();
        themeObserver.disconnect();
      };
    }, []);

    useEffect(() => {
      let frame = 0;
      let mounted = true;
      const draw = () => {
        const canvas = canvasRef.current;
        const context = canvas?.getContext('2d');
        const palette = paletteRef.current;
        if (!canvas || !context || !palette) return;
        const width = canvas.clientWidth;
        const height = canvas.clientHeight;
        const ratioX = canvas.width / Math.max(1, width);
        const ratioY = canvas.height / Math.max(1, height);
        context.setTransform(1, 0, 0, 1, 0, 0);
        context.clearRect(0, 0, canvas.width, canvas.height);
        context.setTransform(ratioX, 0, 0, ratioY, 0, 0);
        context.save();
        context.translate(transformRef.current.x, transformRef.current.y);
        context.scale(transformRef.current.scale, transformRef.current.scale);

        const nodeById = new Map(nodesRef.current.map(node => [node.id, node]));
        const focusId = selectedNodeIdRef.current || hoveredNodeIdRef.current;
        const relatedIds = new Set<string>(focusId ? [focusId] : []);
        if (focusId) {
          edgesRef.current.forEach(edge => {
            if (edge.source === focusId) relatedIds.add(edge.target);
            if (edge.target === focusId) relatedIds.add(edge.source);
          });
        }

        edgesRef.current.forEach(edge => {
          const source = nodeById.get(edge.source);
          const target = nodeById.get(edge.target);
          if (!source || !target) return;
          const active = edge.id === selectedEdgeIdRef.current || Boolean(focusId && (edge.source === focusId || edge.target === focusId));
          context.strokeStyle = active ? palette.primary : palette.borderStrong;
          context.fillStyle = context.strokeStyle;
          context.globalAlpha = focusId && !active ? 0.12 : active ? 0.86 : 0.38;
          context.lineWidth = active ? 2.2 : 1.1;
          context.setLineDash(edge.type === 'needs_adapter' || edge.type === 'complements' ? [7, 5] : []);
          context.beginPath();
          context.moveTo(source.x, source.y);
          context.lineTo(target.x, target.y);
          context.stroke();
          context.setLineDash([]);

          if (edge.directed !== false) {
            const angle = Math.atan2(target.y - source.y, target.x - source.x);
            const radius = nodeRadius(target);
            const arrowX = target.x - Math.cos(angle) * radius;
            const arrowY = target.y - Math.sin(angle) * radius;
            context.beginPath();
            context.moveTo(arrowX, arrowY);
            context.lineTo(arrowX - Math.cos(angle - 0.5) * 8, arrowY - Math.sin(angle - 0.5) * 8);
            context.lineTo(arrowX - Math.cos(angle + 0.5) * 8, arrowY - Math.sin(angle + 0.5) * 8);
            context.closePath();
            context.fill();
          }
          context.globalAlpha = 1;
        });

        nodesRef.current.forEach(node => {
          const selected = selectedNodeIdRef.current === node.id;
          const hovered = hoveredNodeIdRef.current === node.id;
          const unrelated = Boolean(focusId && !relatedIds.has(node.id));
          const radius = nodeRadius(node);
          context.globalAlpha = unrelated ? 0.22 : node.reference ? 0.58 : 1;
          context.fillStyle = node.kind === 'team' ? palette.warning : node.reference ? palette.primarySubtle : palette.primary;
          context.strokeStyle = selected || hovered ? palette.text : palette.border;
          context.lineWidth = selected ? 3 : hovered ? 2.2 : 1.2;
          context.beginPath();
          context.arc(node.x, node.y, radius, 0, Math.PI * 2);
          context.fill();
          context.stroke();

          if (transformRef.current.scale > 0.4 || selected || hovered) {
            context.font = `${selected ? 13 : 11}px Inter, system-ui, sans-serif`;
            context.fillStyle = selected || hovered ? palette.text : palette.textSecondary;
            context.textAlign = 'center';
            context.textBaseline = 'top';
            context.fillText(truncate(node.label, 24), node.x, node.y + radius + 5);
          }
          context.globalAlpha = 1;
        });
        context.restore();
      };

      const tick = () => {
        if (!mounted) return;
        if ((!pointerRef.current || pointerRef.current.kind !== 'node') && layoutTicksRemainingRef.current > 0) {
          stepRelationshipGraphLayout(
            nodesRef.current,
            edgesRef.current,
            canvasRef.current?.clientWidth || 900,
            canvasRef.current?.clientHeight || 620,
            componentsRef.current,
            COMPONENT_CENTER_ATTRACTION_STRENGTH,
          );
          layoutTicksRemainingRef.current -= 1;
        }
        draw();
        frame = window.requestAnimationFrame(tick);
      };
      tick();
      return () => {
        mounted = false;
        window.cancelAnimationFrame(frame);
      };
    }, []);

    const screenToWorld = useCallback((screenX: number, screenY: number) => {
      const transform = transformRef.current;
      return { x: (screenX - transform.x) / transform.scale, y: (screenY - transform.y) / transform.scale };
    }, []);

    const pointerPoint = useCallback((clientX: number, clientY: number) => {
      const rect = canvasRef.current?.getBoundingClientRect();
      return { x: clientX - (rect?.left || 0), y: clientY - (rect?.top || 0) };
    }, []);

    const findNodeAt = useCallback(
      (clientX: number, clientY: number) => {
        const point = pointerPoint(clientX, clientY);
        const world = screenToWorld(point.x, point.y);
        for (let index = nodesRef.current.length - 1; index >= 0; index -= 1) {
          const node = nodesRef.current[index];
          const hitRadius = nodeRadius(node) + 6 / transformRef.current.scale;
          if (Math.hypot(node.x - world.x, node.y - world.y) <= hitRadius) return node;
        }
        return null;
      },
      [pointerPoint, screenToWorld],
    );

    const findEdgeAt = useCallback(
      (clientX: number, clientY: number) => {
        const point = pointerPoint(clientX, clientY);
        const world = screenToWorld(point.x, point.y);
        const nodeById = new Map(nodesRef.current.map(node => [node.id, node]));
        const threshold = 8 / transformRef.current.scale;
        return (
          [...edgesRef.current].reverse().find(edge => {
            const source = nodeById.get(edge.source);
            const target = nodeById.get(edge.target);
            return source && target && pointToSegmentDistance(world.x, world.y, source.x, source.y, target.x, target.y) <= threshold;
          }) || null
        );
      },
      [pointerPoint, screenToWorld],
    );

    return (
      <canvas
        ref={canvasRef}
        className="relationship-graph__canvas"
        role="img"
        tabIndex={0}
        aria-label={ariaLabel}
        onPointerDown={event => {
          const node = findNodeAt(event.clientX, event.clientY);
          event.currentTarget.setPointerCapture(event.pointerId);
          if (node) {
            pointerRef.current = { kind: 'node', pointerId: event.pointerId, nodeId: node.id, moved: false };
            onSelectNode?.(node.id);
            onSelectEdge?.(null);
            return;
          }
          pointerRef.current = {
            kind: 'pan',
            pointerId: event.pointerId,
            clientX: event.clientX,
            clientY: event.clientY,
            originX: transformRef.current.x,
            originY: transformRef.current.y,
            moved: false,
          };
        }}
        onPointerMove={event => {
          const interaction = pointerRef.current;
          if (!interaction) {
            hoveredNodeIdRef.current = findNodeAt(event.clientX, event.clientY)?.id || '';
            event.currentTarget.style.cursor = hoveredNodeIdRef.current ? 'pointer' : 'grab';
            return;
          }
          if (interaction.pointerId !== event.pointerId) return;
          if (interaction.kind === 'node') {
            const point = pointerPoint(event.clientX, event.clientY);
            const world = screenToWorld(point.x, point.y);
            const node = nodesRef.current.find(item => item.id === interaction.nodeId);
            if (node) {
              node.x = world.x;
              node.y = world.y;
              node.vx = 0;
              node.vy = 0;
              interaction.moved = true;
            }
            event.currentTarget.style.cursor = 'grabbing';
            return;
          }
          const dx = event.clientX - interaction.clientX;
          const dy = event.clientY - interaction.clientY;
          transformRef.current.x = interaction.originX + dx;
          transformRef.current.y = interaction.originY + dy;
          interaction.moved = interaction.moved || Math.hypot(dx, dy) > 3;
          event.currentTarget.style.cursor = 'grabbing';
        }}
        onPointerUp={event => {
          const interaction = pointerRef.current;
          pointerRef.current = null;
          event.currentTarget.releasePointerCapture(event.pointerId);
          event.currentTarget.style.cursor = 'grab';
          if (!interaction || interaction.pointerId !== event.pointerId) return;
          if (interaction.kind === 'node') {
            layoutTicksRemainingRef.current = 80;
            return;
          }
          if (interaction.moved) return;
          const edge = findEdgeAt(event.clientX, event.clientY);
          onSelectEdge?.(edge?.id || null);
          onSelectNode?.(null);
        }}
        onPointerCancel={event => {
          pointerRef.current = null;
          event.currentTarget.style.cursor = 'grab';
        }}
        onPointerLeave={event => {
          if (!pointerRef.current) hoveredNodeIdRef.current = '';
          if (!pointerRef.current) event.currentTarget.style.cursor = 'grab';
        }}
        onWheel={event => {
          event.preventDefault();
          const point = pointerPoint(event.clientX, event.clientY);
          zoomAt(event.deltaY < 0 ? 1.12 : 1 / 1.12, point.x, point.y);
        }}
        onDoubleClick={event => {
          const node = findNodeAt(event.clientX, event.clientY);
          if (node) focusNode(node.id);
          else fitView();
        }}
        onKeyDown={event => {
          if (event.key === '+' || event.key === '=') zoomAt(1.2);
          else if (event.key === '-') zoomAt(1 / 1.2);
          else if (event.key === '0') fitView();
          else return;
          event.preventDefault();
        }}
      />
    );
  },
);
