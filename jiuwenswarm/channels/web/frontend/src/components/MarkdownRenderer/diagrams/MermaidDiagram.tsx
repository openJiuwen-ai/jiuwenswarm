import clsx from 'clsx';
import { RotateCcw, ZoomIn, ZoomOut } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { getSvgNaturalHeight, getSvgNaturalWidth } from '../../../utils/svgDimensions';
import { DiagramViewer, type DiagramToolbarAction, type DiagramViewMode } from './DiagramViewer';
import {
  calculateMermaidCanvasLayout,
  clampMermaidScale,
  MERMAID_CANVAS_MIN_HEIGHT,
  MERMAID_CANVAS_TOP_OFFSET,
} from './mermaidLayout';
import { renderMermaidSvg, type MermaidSvgRenderer } from './mermaidRuntime';

type MermaidRenderState = { status: 'loading'; svg: '' } | { status: 'rendered'; svg: string } | { status: 'error'; svg: '' };

export interface MermaidDiagramProps {
  code: string;
  renderSvg?: MermaidSvgRenderer;
  canvasMinHeight?: number;
}

interface Point {
  x: number;
  y: number;
}

interface MermaidSvgDimensions {
  width: number;
  height: number;
}

function normalizeMermaidSvgDimensions(svg: SVGSVGElement): MermaidSvgDimensions | null {
  const width = getSvgNaturalWidth(svg);
  const height = getSvgNaturalHeight(svg);
  if (width <= 0 || height <= 0) return null;

  // Mermaid uses width="100%" for several diagram types. The absolute
  // wrapper has no independent width, so that percentage becomes a
  // shrink-to-fit size before the viewer applies its own scale. Give every
  // diagram the same pixel-sized starting box derived from its viewBox.
  svg.style.width = `${width}px`;
  svg.style.height = `${height}px`;

  return { width, height };
}

function isScrollbarPointer(canvas: HTMLDivElement, clientX: number, clientY: number): boolean {
  const bounds = canvas.getBoundingClientRect();
  const x = clientX - bounds.left;
  const y = clientY - bounds.top;
  const hasVerticalScrollbar = canvas.scrollHeight > canvas.clientHeight;
  const hasHorizontalScrollbar = canvas.scrollWidth > canvas.clientWidth;

  return (hasVerticalScrollbar && x >= canvas.clientWidth) || (hasHorizontalScrollbar && y >= canvas.clientHeight);
}

function centerCanvasScroll(canvas: HTMLDivElement): void {
  canvas.scrollLeft = Math.max(0, Math.round((canvas.scrollWidth - canvas.clientWidth) / 2));
  canvas.scrollTop = Math.max(0, Math.round((canvas.scrollHeight - canvas.clientHeight) / 2));
}

export function MermaidDiagram({ code, renderSvg = renderMermaidSvg, canvasMinHeight }: MermaidDiagramProps): JSX.Element {
  const { t } = useTranslation();
  const diagramId = `mermaid-${useId().replace(/[^A-Za-z0-9_-]/g, '_')}`;
  const [renderState, setRenderState] = useState<MermaidRenderState>({ status: 'loading', svg: '' });
  const [viewMode, setViewMode] = useState<DiagramViewMode>('image');
  const [scale, setScale] = useState(1);
  const [fitScale, setFitScale] = useState(1);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [viewResetKey, setViewResetKey] = useState(0);
  const minimumCanvasHeight = canvasMinHeight ?? MERMAID_CANVAS_MIN_HEIGHT;
  const [canvasHeight, setCanvasHeight] = useState(minimumCanvasHeight);
  const isToolResult = canvasMinHeight !== undefined;
  const isDraggingRef = useRef(false);
  const dragStartRef = useRef<Point>({ x: 0, y: 0 });
  const panStartRef = useRef<Point>({ x: 0, y: 0 });
  const canvasRef = useRef<HTMLDivElement>(null);
  const zoomScrollRef = useRef<Point | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function render(): Promise<void> {
      setRenderState({ status: 'loading', svg: '' });
      try {
        const svg = await renderSvg(diagramId, code);
        if (!cancelled) setRenderState({ status: 'rendered', svg });
      } catch {
        if (!cancelled) setRenderState({ status: 'error', svg: '' });
      }
    }

    void render();
    return () => {
      cancelled = true;
    };
  }, [code, diagramId, renderSvg]);

  useLayoutEffect(() => {
    if (renderState.status !== 'rendered' || viewMode !== 'image') return;
    const svg = canvasRef.current?.querySelector('svg');
    if (!svg) return;
    const renderedSvg = svg;
    const dimensions = normalizeMermaidSvgDimensions(renderedSvg);
    if (!dimensions) return;
    const { width: naturalWidth, height: naturalHeight } = dimensions;
    if (isToolResult) {
      zoomScrollRef.current = null;
      setScale(1);
      setPan({ x: 0, y: 0 });
    }

    function updateDimensions(): void {
      const layout = calculateMermaidCanvasLayout({
        naturalHeight,
        naturalWidth,
        containerWidth: canvasRef.current?.clientWidth ?? 0,
        minCanvasHeight: canvasMinHeight,
      });
      if (!layout) return;

      if (!isToolResult) {
        setFitScale(layout.fitScale);
        setScale(layout.displayScale ?? layout.fitScale);
        setPan({ x: 0, y: 0 });
      }
      setCanvasHeight(layout.canvasHeight);
    }

    updateDimensions();
    const canvas = canvasRef.current;
    if (!canvas) return;

    const observer = new ResizeObserver(updateDimensions);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [canvasMinHeight, isToolResult, renderState.status, renderState.svg, viewMode]);

  useLayoutEffect(() => {
    if (!isToolResult || renderState.status !== 'rendered' || viewMode !== 'image') return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const zoomScroll = zoomScrollRef.current;
    if (zoomScroll) {
      canvas.scrollLeft = Math.max(0, zoomScroll.x);
      canvas.scrollTop = Math.max(0, zoomScroll.y);
      zoomScrollRef.current = null;
      return;
    }
    centerCanvasScroll(canvas);
  }, [canvasHeight, isToolResult, renderState.status, renderState.svg, scale, viewMode, viewResetKey]);

  function startDrag(clientX: number, clientY: number): void {
    isDraggingRef.current = true;
    setIsDragging(true);
    dragStartRef.current = { x: clientX, y: clientY };
    panStartRef.current = { ...pan };
  }

  function moveDrag(clientX: number, clientY: number): void {
    if (!isDraggingRef.current) return;
    const dx = clientX - dragStartRef.current.x;
    const dy = clientY - dragStartRef.current.y;
    setPan({ x: panStartRef.current.x + dx, y: panStartRef.current.y + dy });
  }

  function endDrag(): void {
    isDraggingRef.current = false;
    setIsDragging(false);
  }

  function zoomTo(nextScale: number): void {
    const targetScale = clampMermaidScale(nextScale);
    if (targetScale === scale) return;

    const canvas = canvasRef.current;
    if (canvas) {
      const ratio = targetScale / scale;
      const viewportCenterX = canvas.scrollLeft + canvas.clientWidth / 2;
      const viewportCenterY = canvas.scrollTop + canvas.clientHeight / 2;
      zoomScrollRef.current = {
        x: Math.round((viewportCenterX - pan.x) * ratio + pan.x - canvas.clientWidth / 2),
        y: Math.round((viewportCenterY - pan.y) * ratio + pan.y - canvas.clientHeight / 2),
      };
    }
    setScale(targetScale);
  }

  function resetView(): void {
    zoomScrollRef.current = null;
    setScale(1);
    setPan({ x: 0, y: 0 });
    setViewResetKey(currentKey => currentKey + 1);
  }

  if (renderState.status === 'error') {
    return (
      <pre className="mermaid-error" data-mermaid-status="error" data-testid="markdown-mermaid-error">
        <code>{code}</code>
      </pre>
    );
  }

  const rendered = renderState.status === 'rendered';
  const toolbarActions: DiagramToolbarAction[] = [];
  if (rendered && viewMode === 'image') {
    toolbarActions.push(
      {
        id: 'zoom-in',
        title: t('mermaid.zoomIn'),
        icon: <ZoomIn size={15} />,
        onClick: () => {
          if (isToolResult) {
            zoomTo(scale + 0.25);
          } else {
            setScale(currentScale => clampMermaidScale(currentScale + 0.25));
          }
        },
      },
      {
        id: 'zoom-out',
        title: t('mermaid.zoomOut'),
        icon: <ZoomOut size={15} />,
        onClick: () => {
          if (isToolResult) {
            zoomTo(scale - 0.25);
          } else {
            setScale(currentScale => clampMermaidScale(currentScale - 0.25));
          }
        },
      },
      {
        id: 'fit-view',
        title: t('mermaid.fitView'),
        icon: <RotateCcw size={15} />,
        onClick: () => {
          if (isToolResult) {
            resetView();
          } else {
            setScale(fitScale);
            setPan({ x: 0, y: 0 });
          }
        },
      },
    );
  }

  const panTransform = `translate(${pan.x}px, ${pan.y}px)`;
  const wrapperStyle: CSSProperties = isToolResult
    ? {
        width: 'max-content',
        minWidth: '100%',
        height: 'max-content',
        minHeight: '100%',
        padding: `${MERMAID_CANVAS_TOP_OFFSET}px 24px`,
        display: 'flex',
        flex: '0 0 auto',
        alignItems: 'center',
        justifyContent: 'center',
        boxSizing: 'border-box',
        transformOrigin: 'top left',
        transform: `${panTransform} scale(${scale})`,
      }
    : {
        top: '50%',
        transformOrigin: 'center center',
        transform: `translate(-50%, -50%) ${panTransform} scale(${scale})`,
      };

  return (
    <DiagramViewer
      className="mermaid-diagram"
      data-mermaid-status={renderState.status}
      viewMode={viewMode}
      onViewModeChange={setViewMode}
      toolbarActions={toolbarActions}
      statusText={rendered ? undefined : t('mermaid.rendering')}
      exportConfig={{
        sourceCode: code,
        renderedSvg: renderState.svg,
        imageFilename: 'diagram.png',
        downloadEnabled: rendered,
      }}
    >
      {viewMode === 'image' ? (
        <div
          ref={canvasRef}
          className={clsx('mermaid-canvas', isToolResult && 'mermaid-canvas--tool-result', isDragging && 'mermaid-canvas--dragging')}
          style={{ height: canvasHeight }}
          aria-busy={!rendered}
          data-testid="markdown-mermaid-canvas"
          onMouseDown={event => {
            if (isToolResult && isScrollbarPointer(event.currentTarget, event.clientX, event.clientY)) return;
            event.preventDefault();
            startDrag(event.clientX, event.clientY);
          }}
          onMouseMove={event => moveDrag(event.clientX, event.clientY)}
          onMouseUp={endDrag}
          onMouseLeave={endDrag}
          onTouchStart={event => {
            const touch = event.touches[0];
            startDrag(touch.clientX, touch.clientY);
          }}
          onTouchMove={event => {
            const touch = event.touches[0];
            moveDrag(touch.clientX, touch.clientY);
          }}
          onTouchEnd={endDrag}
        >
          {rendered && <div className="mermaid-svg-wrapper" style={wrapperStyle} data-testid="markdown-mermaid-svg" dangerouslySetInnerHTML={{ __html: renderState.svg }} />}
        </div>
      ) : (
        <div className="mermaid-code-view" data-testid="markdown-mermaid-code-view">
          <pre>
            <code>{code}</code>
          </pre>
        </div>
      )}
    </DiagramViewer>
  );
}
