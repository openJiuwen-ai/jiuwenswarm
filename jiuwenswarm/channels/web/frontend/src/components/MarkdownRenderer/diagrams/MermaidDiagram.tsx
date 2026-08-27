import clsx from 'clsx';
import { RotateCcw, ZoomIn, ZoomOut } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import { getSvgNaturalHeight, getSvgNaturalWidth } from '../../../utils/svgDimensions';
import { DiagramViewer, type DiagramToolbarAction, type DiagramViewMode } from './DiagramViewer';
import { calculateMermaidCanvasLayout, clampMermaidScale, MERMAID_CANVAS_MIN_HEIGHT } from './mermaidLayout';
import { renderMermaidSvg, type MermaidSvgRenderer } from './mermaidRuntime';

type MermaidRenderState = { status: 'loading'; svg: '' } | { status: 'rendered'; svg: string } | { status: 'error'; svg: '' };

export interface MermaidDiagramProps {
  code: string;
  renderSvg?: MermaidSvgRenderer;
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

export function MermaidDiagram({ code, renderSvg = renderMermaidSvg }: MermaidDiagramProps): JSX.Element {
  const { t } = useTranslation();
  const diagramId = `mermaid-${useId().replace(/[^A-Za-z0-9_-]/g, '_')}`;
  const [renderState, setRenderState] = useState<MermaidRenderState>({ status: 'loading', svg: '' });
  const [viewMode, setViewMode] = useState<DiagramViewMode>('image');
  const [scale, setScale] = useState(1);
  const [fitScale, setFitScale] = useState(1);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [canvasHeight, setCanvasHeight] = useState(MERMAID_CANVAS_MIN_HEIGHT);
  const isDraggingRef = useRef(false);
  const dragStartRef = useRef<Point>({ x: 0, y: 0 });
  const panStartRef = useRef<Point>({ x: 0, y: 0 });
  const canvasRef = useRef<HTMLDivElement>(null);

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

    function updateDimensions(): void {
      const layout = calculateMermaidCanvasLayout({
        naturalHeight,
        naturalWidth,
        containerWidth: canvasRef.current?.clientWidth ?? 0,
      });
      if (!layout) return;

      setFitScale(layout.fitScale);
      setScale(layout.displayScale);
      setPan({ x: 0, y: 0 });
      setCanvasHeight(layout.canvasHeight);
    }

    updateDimensions();
    const canvas = canvasRef.current;
    if (!canvas) return;

    const observer = new ResizeObserver(updateDimensions);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [renderState.status, renderState.svg, viewMode]);

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
        onClick: () => setScale(currentScale => clampMermaidScale(currentScale + 0.25)),
      },
      {
        id: 'zoom-out',
        title: t('mermaid.zoomOut'),
        icon: <ZoomOut size={15} />,
        onClick: () => setScale(currentScale => clampMermaidScale(currentScale - 0.25)),
      },
      {
        id: 'fit-view',
        title: t('mermaid.fitView'),
        icon: <RotateCcw size={15} />,
        onClick: () => {
          setScale(fitScale);
          setPan({ x: 0, y: 0 });
        },
      },
    );
  }

  const panTransform = `translate(${pan.x}px, ${pan.y}px)`;
  const wrapperStyle: CSSProperties = {
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
          className={clsx('mermaid-canvas', isDragging && 'mermaid-canvas--dragging')}
          style={{ height: canvasHeight }}
          aria-busy={!rendered}
          data-testid="markdown-mermaid-canvas"
          onMouseDown={event => {
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
