import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { AlertCircle, ChevronLeft, ChevronRight, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import { categoryCenter, categoryPoint, clusteredCategoryBand, ensureNonZeroAxisSpan, linearPosition, spanFromBaseline } from './chartGeometry';
import { officeFontStack } from './officeFontStack';
import {
  MAX_PRESENTATION_PREVIEW_BYTES,
  presentationLineHeight,
  type PresentationChart,
  type PresentationData,
  type PresentationFill,
  type PresentationImage,
  type PresentationNode,
  type PresentationParagraph,
  type PresentationShape,
  type PresentationSlide,
  type PresentationSpacing,
  type PresentationTable,
  type PresentationText,
  type PresentationWorkerResponse,
} from './pptxPreviewModel';

type PreviewState = 'loading' | 'ready' | 'error' | 'resource-limit' | 'too-large';

function PreviewMessage({ children, danger = false }: { children: string; danger?: boolean }) {
  return (
    <div
      className={clsx('flex h-full min-h-[240px] items-center justify-center gap-2 p-6 text-sm', danger ? 'text-danger' : 'text-text-muted')}
      role={danger ? 'alert' : undefined}
    >
      <AlertCircle size={16} />
      <span>{children}</span>
    </div>
  );
}

export function PresentationPreview({
  artifactId,
  url,
  title,
  size,
  onStructureInvalidChange,
}: {
  artifactId: string;
  url: string;
  title: string;
  size?: number;
  onStructureInvalidChange?: (artifactId: string, invalid: boolean) => void;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<PreviewState>(size !== undefined && size > MAX_PRESENTATION_PREVIEW_BYTES ? 'too-large' : 'loading');
  const [presentation, setPresentation] = useState<PresentationData | null>(null);

  useEffect(() => {
    onStructureInvalidChange?.(artifactId, false);
    if (size !== undefined && size > MAX_PRESENTATION_PREVIEW_BYTES) {
      setState('too-large');
      setPresentation(null);
      return;
    }
    const controller = new AbortController();
    const worker = new Worker(new URL('./pptxPreview.worker.ts', import.meta.url), { type: 'module' });
    let cancelled = false;
    setState('loading');
    setPresentation(null);
    worker.onmessage = (event: MessageEvent<PresentationWorkerResponse>) => {
      if (cancelled) return;
      if (event.data.type === 'ready') {
        setPresentation(event.data.presentation);
        setState('ready');
        onStructureInvalidChange?.(
          artifactId,
          event.data.presentation.slides.some(slide => slide.status === 'invalid'),
        );
      } else {
        console.error('Failed to parse presentation preview', event.data.message);
        setState(event.data.code === 'resource-limit' ? 'resource-limit' : 'error');
      }
      worker.terminate();
    };
    worker.onerror = event => {
      if (!cancelled) {
        console.error('Presentation preview worker failed', event.message);
        setState('error');
      }
      worker.terminate();
    };
    void fetch(url, { cache: 'no-store', signal: controller.signal })
      .then(async response => {
        const contentType = (response.headers.get('content-type') ?? '').toLowerCase();
        const contentLength = Number(response.headers.get('content-length'));
        if (!response.ok || contentType.includes('text/html') || contentType.includes('application/json'))
          throw new Error(`PPTX request failed with HTTP ${response.status}`);
        if (Number.isFinite(contentLength) && contentLength > MAX_PRESENTATION_PREVIEW_BYTES) throw new Error('presentation_too_large');
        return readLimitedResponse(response);
      })
      .then(buffer => {
        if (!cancelled) worker.postMessage({ type: 'parse', buffer }, [buffer]);
      })
      .catch(error => {
        if (cancelled || controller.signal.aborted) return;
        setState(error instanceof Error && error.message === 'presentation_too_large' ? 'too-large' : 'error');
        worker.terminate();
      });
    return () => {
      cancelled = true;
      controller.abort();
      worker.terminate();
    };
  }, [artifactId, onStructureInvalidChange, size, url]);

  if (state === 'loading') {
    return (
      <div
        className="flex h-full min-h-[240px] items-center justify-center gap-2 text-sm text-text-muted"
        aria-label={title}
        data-testid="artifact-presentation-preview"
        data-variant="loading"
      >
        <LoaderCircle className="animate-spin" size={16} />
        {t('common.loading')}
      </div>
    );
  }
  if (state === 'too-large')
    return (
      <div className="h-full" aria-label={title} data-testid="artifact-presentation-preview" data-variant="too-large">
        <PreviewMessage danger>{t('artifacts.presentationTooLarge', { size: '50 MiB' })}</PreviewMessage>
      </div>
    );
  if (state === 'resource-limit')
    return (
      <div className="h-full" aria-label={title} data-testid="artifact-presentation-preview" data-variant="resource-limit">
        <PreviewMessage danger>{t('artifacts.presentationResourceLimitExceeded')}</PreviewMessage>
      </div>
    );
  if (state === 'error' || !presentation)
    return (
      <div className="h-full" aria-label={title} data-testid="artifact-presentation-preview" data-variant="error">
        <PreviewMessage danger>{t('artifacts.presentationPreviewFailed')}</PreviewMessage>
      </div>
    );
  return <PresentationViewer presentation={presentation} title={title} />;
}

async function readLimitedResponse(response: Response): Promise<ArrayBuffer> {
  if (!response.body) throw new Error('presentation_response_body_missing');
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_PRESENTATION_PREVIEW_BYTES) {
      await reader.cancel();
      throw new Error('presentation_too_large');
    }
    chunks.push(value);
  }
  const result = new Uint8Array(size);
  let offset = 0;
  chunks.forEach(chunk => {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  });
  return result.buffer;
}

function PresentationViewer({ presentation, title }: { presentation: PresentationData; title: string }) {
  const { t } = useTranslation();
  const [slideIndex, setSlideIndex] = useState(0);
  const canvasRef = useRef<HTMLDivElement>(null);
  const thumbnailListRef = useRef<HTMLDivElement>(null);
  const thumbnailListId = useId();
  const [thumbnailScrollState, setThumbnailScrollState] = useState({ isOverflowing: false, canScrollBack: false, canScrollForward: false });
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const activeSlide = presentation.slides[slideIndex] ?? presentation.slides[0];
  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    const update = () => {
      const { width, height } = element.getBoundingClientRect();
      setViewport({ width, height });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    thumbnailListRef.current?.querySelector<HTMLElement>('[aria-current="page"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [slideIndex]);
  const updateThumbnailScrollState = useCallback(() => {
    const element = thumbnailListRef.current;
    if (!element) return;
    const isOverflowing = element.scrollWidth > element.clientWidth;
    const next = {
      isOverflowing,
      canScrollBack: isOverflowing && element.scrollLeft > 0,
      canScrollForward: isOverflowing && element.scrollLeft < element.scrollWidth - element.clientWidth,
    };
    setThumbnailScrollState(current =>
      current.isOverflowing === next.isOverflowing &&
      current.canScrollBack === next.canScrollBack &&
      current.canScrollForward === next.canScrollForward
        ? current
        : next,
    );
  }, []);
  useLayoutEffect(() => {
    const element = thumbnailListRef.current;
    if (!element) return;
    updateThumbnailScrollState();
    const observer = new ResizeObserver(updateThumbnailScrollState);
    observer.observe(element);
    return () => observer.disconnect();
  }, [presentation.slides.length, updateThumbnailScrollState]);
  const scrollThumbnailList = useCallback((direction: -1 | 1) => {
    const element = thumbnailListRef.current;
    if (!element) return;
    element.scrollBy({ left: direction * element.clientWidth, behavior: 'smooth' });
  }, []);
  const fit = Math.min(Math.max(0.05, (viewport.width - 48) / presentation.width), Math.max(0.05, (viewport.height - 48) / presentation.height));
  const scale = fit;
  const goTo = (next: number) => setSlideIndex(Math.min(Math.max(next, 0), presentation.slides.length - 1));
  return (
    <div
      className="flex h-full min-h-0 w-full flex-col overflow-hidden border border-border bg-card"
      aria-label={title}
      data-testid="artifact-presentation-preview"
      data-variant="ready"
    >
      <section ref={canvasRef} className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="artifact-presentation-canvas">
        <div className="min-h-0 flex-1 overflow-auto bg-bg-muted p-6" data-testid="artifact-presentation-slide-stage">
          <div
            className="flex min-h-full min-w-full items-center justify-center"
            style={{ width: Math.max(viewport.width - 48, presentation.width * scale), height: Math.max(viewport.height - 48, presentation.height * scale) }}
          >
            <SlideCanvas presentation={presentation} slide={activeSlide} scale={scale} />
          </div>
        </div>
      </section>
      <nav className="shrink-0 border-t border-border bg-panel" aria-label={t('artifacts.presentationSlides')} data-testid="artifact-presentation-thumbnails">
        <div className="relative">
          <div
            ref={thumbnailListRef}
            id={thumbnailListId}
            className="flex min-w-0 gap-2 overflow-x-auto overflow-y-hidden p-2 [overscroll-behavior-inline:contain]"
            data-testid="artifact-presentation-thumbnail-list"
            onScroll={updateThumbnailScrollState}
          >
            {presentation.slides.map((slide, index) => (
              <button
                key={slide.id}
                type="button"
                className={clsx(
                  'flex w-fit shrink-0 flex-col items-center gap-1 rounded border p-1 transition-colors',
                  index === slideIndex ? 'border-accent bg-accent-subtle' : 'border-transparent hover:border-border hover:bg-secondary',
                )}
                data-testid="artifact-presentation-thumbnail"
                data-variant={slide.id}
                onClick={() => goTo(index)}
                aria-current={index === slideIndex ? 'page' : undefined}
                aria-label={t('artifacts.presentationGoToSlide', { index: index + 1 })}
              >
                <SlideThumbnail presentation={presentation} slide={slide} active={Math.abs(index - slideIndex) <= 1} />
                <span className="text-[11px] leading-none tabular-nums text-text-muted" aria-hidden="true">
                  {index + 1}
                </span>
              </button>
            ))}
          </div>
          {thumbnailScrollState.isOverflowing && (
            <>
              <button
                type="button"
                className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full border border-border bg-card p-1 text-text-muted shadow-sm transition-colors hover:bg-secondary hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
                data-testid="artifact-presentation-thumbnail-prev"
                onClick={() => scrollThumbnailList(-1)}
                disabled={!thumbnailScrollState.canScrollBack}
                aria-controls={thumbnailListId}
                aria-label={t('artifacts.presentationScrollThumbnailsPrevious')}
              >
                <ChevronLeft size={16} aria-hidden="true" />
              </button>
              <button
                type="button"
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full border border-border bg-card p-1 text-text-muted shadow-sm transition-colors hover:bg-secondary hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:cursor-not-allowed disabled:opacity-40"
                data-testid="artifact-presentation-thumbnail-next"
                onClick={() => scrollThumbnailList(1)}
                disabled={!thumbnailScrollState.canScrollForward}
                aria-controls={thumbnailListId}
                aria-label={t('artifacts.presentationScrollThumbnailsNext')}
              >
                <ChevronRight size={16} aria-hidden="true" />
              </button>
            </>
          )}
        </div>
      </nav>
    </div>
  );
}

function SlideThumbnail({ presentation, slide, active }: { presentation: PresentationData; slide: PresentationSlide; active: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(active);
  useEffect(() => {
    if (active) {
      setVisible(true);
      return;
    }
    setVisible(false);
    const element = ref.current;
    if (!element || !('IntersectionObserver' in window)) return;
    const observer = new IntersectionObserver(entries => setVisible(entries.some(entry => entry.isIntersecting)), { rootMargin: '0px 160px' });
    observer.observe(element);
    return () => observer.disconnect();
  }, [active]);
  const scale = Math.min(134 / presentation.width, 82 / presentation.height);
  return (
    <div
      ref={ref}
      className="relative flex shrink-0 items-center justify-center overflow-hidden bg-white"
      style={{ width: presentation.width * scale, height: presentation.height * scale }}
      aria-hidden="true"
    >
      {visible || active ? <SlideCanvas presentation={presentation} slide={slide} scale={scale} /> : null}
    </div>
  );
}

function SlideCanvas({ presentation, slide, scale }: { presentation: PresentationData; slide: PresentationSlide; scale: number }) {
  const background = fillStyle(slide.background);
  return (
    <div className="shrink-0" style={{ width: presentation.width * scale, height: presentation.height * scale }}>
      <div
        className="relative bg-white shadow-sm"
        style={{ width: presentation.width, height: presentation.height, ...background, transform: `scale(${scale})`, transformOrigin: 'top left' }}
      >
        {slide.status !== 'invalid' && slide.nodes.map(node => <SlideNode key={node.id} node={node} />)}
      </div>
    </div>
  );
}

function SlideNode({ node }: { node: PresentationNode }) {
  if (node.type === 'shape') return <ShapeNode shape={node} />;
  if (node.type === 'image') return <ImageNode image={node} />;
  if (node.type === 'table') return <TableNode table={node} />;
  if (node.type === 'chart') return <ChartNode chart={node} />;
  return <UnsupportedNode node={node} />;
}

function nodeStyle(node: PresentationNode): CSSProperties {
  const transforms = [`rotate(${node.rotation ?? 0}deg)`];
  if (node.flipH) transforms.push('scaleX(-1)');
  if (node.flipV) transforms.push('scaleY(-1)');
  return {
    position: 'absolute',
    left: node.x,
    top: node.y,
    width: node.width,
    height: node.height,
    transform: transforms.join(' '),
    transformOrigin: 'center center',
  };
}

function ShapeNode({ shape }: { shape: PresentationShape }) {
  return (
    <div style={nodeStyle(shape)}>
      <ShapeVector shape={shape} />
      {shape.text && <TextBox text={shape.text} />}
    </div>
  );
}

function ShapeVector({ shape }: { shape: PresentationShape }) {
  const gradientId = useId().replace(/:/g, '');
  const width = Math.max(0.001, shape.width);
  const height = Math.max(0.001, shape.height);
  const fill = shape.fill.kind === 'gradient' ? `url(#${gradientId})` : shape.fill.kind === 'solid' ? shape.fill.color : 'transparent';
  const opacity = shape.fill.kind === 'solid' ? 1 - (shape.fill.transparency ?? 0) : 1;
  const stroke = shape.stroke?.color ?? 'none';
  const dash = shape.stroke?.dash;
  const common = { fill, fillOpacity: opacity, stroke, strokeWidth: shape.stroke?.width ?? 0, strokeDasharray: dash };
  return (
    <svg className="absolute inset-0 h-full w-full overflow-visible" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      {shape.fill.kind === 'gradient' && (
        <defs>
          <linearGradient id={gradientId} gradientTransform={`rotate(${shape.fill.angle})`}>
            <>
              {shape.fill.stops.map(stop => (
                <stop key={stop.offset} offset={`${stop.offset * 100}%`} stopColor={stop.color} stopOpacity={1 - (stop.transparency ?? 0)} />
              ))}
            </>
          </linearGradient>
        </defs>
      )}
      <ShapePath shape={shape} common={common} />
    </svg>
  );
}

function ShapePath({ shape, common }: { shape: PresentationShape; common: Record<string, string | number | undefined> }) {
  const { geometry } = shape;
  const width = Math.max(0.001, shape.width);
  const height = Math.max(0.001, shape.height);
  const points = (values: Array<[number, number]>) => values.map(([x, y]) => `${x * width},${y * height}`).join(' ');
  if (geometry === 'ellipse') return <ellipse cx={width / 2} cy={height / 2} rx={width / 2} ry={height / 2} {...common} />;
  if (geometry === 'roundRect') {
    const adjustment = Math.min(50_000, Math.max(0, shape.adjustments?.adj ?? 16_667));
    const radius = (Math.min(width, height) * adjustment) / 100_000;
    return <rect x="0" y="0" width={width} height={height} rx={radius} ry={radius} {...common} />;
  }
  if (geometry === 'triangle')
    return (
      <polygon
        points={points([
          [0.5, 0],
          [1, 1],
          [0, 1],
        ])}
        {...common}
      />
    );
  if (geometry === 'rtTriangle')
    return (
      <polygon
        points={points([
          [0, 0],
          [1, 1],
          [0, 1],
        ])}
        {...common}
      />
    );
  if (geometry === 'diamond')
    return (
      <polygon
        points={points([
          [0.5, 0],
          [1, 0.5],
          [0.5, 1],
          [0, 0.5],
        ])}
        {...common}
      />
    );
  if (geometry === 'parallelogram')
    return (
      <polygon
        points={points([
          [0.2, 0],
          [1, 0],
          [0.8, 1],
          [0, 1],
        ])}
        {...common}
      />
    );
  if (geometry === 'trapezoid')
    return (
      <polygon
        points={points([
          [0.2, 0],
          [0.8, 0],
          [1, 1],
          [0, 1],
        ])}
        {...common}
      />
    );
  if (geometry === 'hexagon')
    return (
      <polygon
        points={points([
          [0.25, 0],
          [0.75, 0],
          [1, 0.5],
          [0.75, 1],
          [0.25, 1],
          [0, 0.5],
        ])}
        {...common}
      />
    );
  if (geometry === 'pentagon')
    return (
      <polygon
        points={points([
          [0.5, 0],
          [1, 0.38],
          [0.81, 1],
          [0.19, 1],
          [0, 0.38],
        ])}
        {...common}
      />
    );
  if (geometry === 'chevron')
    return (
      <polygon
        points={points([
          [0, 0],
          [0.58, 0],
          [1, 0.5],
          [0.58, 1],
          [0, 1],
          [0.42, 0.5],
        ])}
        {...common}
      />
    );
  if (geometry === 'plus')
    return (
      <polygon
        points={points([
          [0.35, 0],
          [0.65, 0],
          [0.65, 0.35],
          [1, 0.35],
          [1, 0.65],
          [0.65, 0.65],
          [0.65, 1],
          [0.35, 1],
          [0.35, 0.65],
          [0, 0.65],
          [0, 0.35],
          [0.35, 0.35],
        ])}
        {...common}
      />
    );
  if (geometry === 'minus') return <rect x="0" y={height * 0.35} width={width} height={height * 0.3} {...common} />;
  if (geometry === 'lineVertical') return <line x1={width / 2} y1="0" x2={width / 2} y2={height} {...common} />;
  if (geometry === 'line' || geometry.includes('Connector')) return <line x1="0" y1={height / 2} x2={width} y2={height / 2} {...common} />;
  return <rect x="0" y="0" width={width} height={height} {...common} />;
}

function TextBox({ text }: { text: PresentationText }) {
  const frameRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const frame = frameRef.current;
    const content = contentRef.current;
    if (!frame || !content) return;
    const fit = () => {
      frame.style.setProperty('--ppt-text-scale', '1');
      if (text.autoFit !== 'shrink') {
        frame.dataset.autofitScale = '1';
        return;
      }
      const computed = getComputedStyle(frame);
      const availableWidth = frame.clientWidth - Number.parseFloat(computed.paddingLeft) - Number.parseFloat(computed.paddingRight);
      const availableHeight = frame.clientHeight - Number.parseFloat(computed.paddingTop) - Number.parseFloat(computed.paddingBottom);
      const fits = (scale: number) => {
        frame.style.setProperty('--ppt-text-scale', String(scale));
        return content.scrollWidth <= availableWidth + 0.5 && content.scrollHeight <= availableHeight + 0.5;
      };
      if (fits(1)) {
        frame.dataset.autofitScale = '1';
        return;
      }
      let lower = 0.01;
      let upper = 1;
      for (let iteration = 0; iteration < 12; iteration += 1) {
        const candidate = (lower + upper) / 2;
        if (fits(candidate)) lower = candidate;
        else upper = candidate;
      }
      frame.style.setProperty('--ppt-text-scale', String(lower));
      frame.dataset.autofitScale = String(lower);
    };
    fit();
    let cancelled = false;
    void document.fonts?.ready.then(() => {
      if (!cancelled) fit();
    });
    return () => {
      cancelled = true;
    };
  }, [text]);
  const style: CSSProperties = {
    position: 'absolute',
    inset: 0,
    display: 'flex',
    flexDirection: 'column',
    justifyContent: text.anchor === 'middle' ? 'center' : text.anchor === 'bottom' ? 'flex-end' : 'flex-start',
    boxSizing: 'border-box',
    padding: `${text.margin.top}px ${text.margin.right}px ${text.margin.bottom}px ${text.margin.left}px`,
    overflow: text.autoFit === 'resize' ? 'visible' : 'hidden',
    color: text.color ?? '#000000',
    fontFamily: officeFontStack(text.fontFamily, text.eastAsianFontFamily, text.complexScriptFontFamily),
    fontSize: scaledFontSize(text.fontSize),
    fontSynthesis: 'none',
    writingMode: text.vertical ? (text.verticalReverse ? 'vertical-lr' : 'vertical-rl') : undefined,
  };
  return (
    <div ref={frameRef} style={style}>
      <div ref={contentRef} style={{ width: '100%', flexShrink: 0 }}>
        {text.paragraphs.map((paragraph, index) => (
          <TextParagraph key={index} paragraph={paragraph} defaults={text} />
        ))}
      </div>
    </div>
  );
}

function TextParagraph({ paragraph, defaults }: { paragraph: PresentationParagraph; defaults: PresentationText }) {
  const largestFontSize = Math.max(defaults.fontSize ?? 0, ...paragraph.runs.map(run => run.fontSize ?? 0)) || undefined;
  const hangingIndent = paragraph.bullet && (paragraph.indent ?? 0) < 0 ? -(paragraph.indent ?? 0) : undefined;
  const style: CSSProperties = {
    margin: 0,
    paddingLeft: paragraph.marginLeft,
    textIndent: paragraph.indent,
    textAlign: paragraph.align,
    marginTop: paragraphSpacing(paragraph.spaceBefore),
    marginBottom: paragraphSpacing(paragraph.spaceAfter),
    lineHeight: presentationLineHeight(paragraph.lineSpacing),
    fontSize: scaledFontSize(largestFontSize),
    whiteSpace: 'pre-wrap',
    lineBreak: 'strict',
    overflowWrap: 'normal',
    wordBreak: 'normal',
  };
  const content = paragraph.runs.map((run, index) => <TextRun key={index} run={run} defaults={defaults} />);
  if (!paragraph.bullet) return <p style={style}>{content}</p>;
  return (
    <p style={style}>
      <span style={{ display: 'inline-block', textIndent: 0, width: hangingIndent, marginRight: hangingIndent === undefined ? '0.4em' : undefined }}>
        {paragraph.bullet.kind === 'number' ? '1.' : paragraph.bullet.value}
      </span>
      {content}
    </p>
  );
}

function TextRun({ run, defaults }: { run: PresentationParagraph['runs'][number]; defaults: PresentationText }) {
  const fontSize = run.fontSize ?? defaults.fontSize;
  const style: CSSProperties = {
    color: run.color ?? defaults.color,
    fontFamily: officeFontStack(
      run.fontFamily ?? defaults.fontFamily,
      run.eastAsianFontFamily ?? defaults.eastAsianFontFamily,
      run.complexScriptFontFamily ?? defaults.complexScriptFontFamily,
    ),
    fontSize: scaledFontSize(fontSize),
    letterSpacing: run.characterSpacing,
    fontKerning: run.kerningThreshold !== undefined && fontSize !== undefined && fontSize < run.kerningThreshold ? 'none' : 'normal',
    fontSynthesis: 'none',
    fontWeight: run.bold ? 700 : undefined,
    fontStyle: run.italic ? 'italic' : undefined,
    textDecoration: run.underline ? 'underline' : undefined,
    verticalAlign: run.baseline ? `${run.baseline / 1_000}%` : undefined,
    whiteSpace: 'pre-wrap',
  };
  return <span style={style}>{run.text}</span>;
}

function paragraphSpacing(spacing: PresentationSpacing | undefined): string | number | undefined {
  if (!spacing) return undefined;
  return spacing.kind === 'absolute' ? spacing.value : `${spacing.value}em`;
}

function scaledFontSize(value: number | undefined): string | undefined {
  return value === undefined ? undefined : `calc(${value}px * var(--ppt-text-scale, 1))`;
}

function ImageNode({ image }: { image: PresentationImage }) {
  const [source, setSource] = useState<string>();
  useEffect(() => {
    const objectUrl = URL.createObjectURL(image.image);
    setSource(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [image.image]);
  const crop = image.crop;
  const visibleWidth = Math.max(0.001, 1 - (crop?.left ?? 0) - (crop?.right ?? 0));
  const visibleHeight = Math.max(0.001, 1 - (crop?.top ?? 0) - (crop?.bottom ?? 0));
  return (
    <div style={{ ...nodeStyle(image), overflow: 'hidden' }}>
      {source && (
        <img
          src={source}
          alt={image.alt}
          draggable={false}
          className="absolute max-w-none"
          style={{
            width: `${100 / visibleWidth}%`,
            height: `${100 / visibleHeight}%`,
            left: `${(-(crop?.left ?? 0) / visibleWidth) * 100}%`,
            top: `${(-(crop?.top ?? 0) / visibleHeight) * 100}%`,
          }}
        />
      )}
    </div>
  );
}

function TableNode({ table }: { table: PresentationTable }) {
  const maximumCells = Math.max(1, ...table.rows.map(row => row.cells.length));
  const columns = table.columns.length > 0 ? table.columns : Array.from({ length: maximumCells }, () => table.width / maximumCells);
  return (
    <div
      style={{
        ...nodeStyle(table),
        display: 'grid',
        gridTemplateColumns: columns.map(width => `${width}px`).join(' '),
        gridTemplateRows: table.rows.map(row => `${row.height}px`).join(' '),
        overflow: 'hidden',
      }}
    >
      {table.rows.flatMap((row, rowIndex) =>
        row.cells.map((cell, cellIndex) => {
          if (cell.merged) return null;
          return (
            <div
              key={`${rowIndex}:${cellIndex}`}
              style={{
                position: 'relative',
                boxSizing: 'border-box',
                minWidth: 0,
                minHeight: 0,
                gridColumn: `span ${cell.colSpan ?? 1}`,
                gridRow: `span ${cell.rowSpan ?? 1}`,
                border: cell.stroke ? `${cell.stroke.width}px solid ${cell.stroke.color}` : '1px solid #B7B7B7',
                ...fillStyle(cell.fill),
                overflow: 'hidden',
              }}
            >
              {cell.text && <TextBox text={cell.text} />}
            </div>
          );
        }),
      )}
    </div>
  );
}

function ChartNode({ chart }: { chart: PresentationChart }) {
  const allValues = chart.series.flatMap(series => series.values);
  if (allValues.length === 0) return <UnsupportedNode node={{ ...chart, type: 'unsupported', feature: 'chart without data' }} />;
  const width = 800;
  const height = 480;
  const plot = { left: chart.chartType === 'bar' ? 145 : 78, top: chart.title ? 80 : 38, right: 716, bottom: 410 };
  const maximum = Math.max(0, ...allValues);
  const minimum = Math.min(0, ...allValues);
  const { maximum: axisMaximum } = ensureNonZeroAxisSpan(minimum, maximum, 1);
  const span = axisMaximum - minimum;
  const categoryCount = Math.max(1, ...chart.series.map(series => Math.max(series.categories.length, series.values.length)));
  const categories =
    chart.series.find(series => series.categories.length > 0)?.categories ?? Array.from({ length: categoryCount }, (_, index) => String(index + 1));
  const colors = ['#4472C4', '#ED7D31', '#A5A5A5', '#FFC000', '#5B9BD5', '#70AD47'];
  const xAtCategory = (index: number) =>
    chart.chartType === 'line' ? categoryPoint(index, categoryCount, plot.left, plot.right) : categoryCenter(index, categoryCount, plot.left, plot.right);
  const yAtCategory = (index: number) => categoryCenter(index, categoryCount, plot.top, plot.bottom);
  const xAtValue = (value: number) => linearPosition(value, minimum, axisMaximum, plot.left, plot.right);
  const yAtValue = (value: number) => linearPosition(value, minimum, axisMaximum, plot.bottom, plot.top);
  const zeroX = xAtValue(0);
  const zeroY = yAtValue(0);
  const chartLabel = chart.title || chart.name || 'Chart';
  const legendSeries = chart.series.map((series, seriesIndex) => ({
    series,
    seriesIndex,
    label: series.name || `Series ${seriesIndex + 1}`,
  }));
  return (
    <figure className="absolute m-0 overflow-hidden bg-white text-black" style={nodeStyle(chart)}>
      <svg className="h-full w-full" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img" aria-label={chartLabel}>
        <rect width={width} height={height} fill="#FFFFFF" />
        {chart.title && (
          <text x={width / 2} y="38" textAnchor="middle" fontSize="25" fontWeight="600">
            {chart.title}
          </text>
        )}
        {[0, 1, 2, 3, 4].map(index => {
          const value = minimum + (span * index) / 4;
          if (chart.chartType === 'bar') {
            const x = xAtValue(value);
            return (
              <g key={index}>
                <line x1={x} x2={x} y1={plot.top} y2={plot.bottom} stroke="#D9D9D9" />
                <text x={x} y={plot.bottom + 22} textAnchor="middle" fontSize="12">
                  {Number(value.toFixed(2))}
                </text>
              </g>
            );
          }
          const y = yAtValue(value);
          return (
            <g key={index}>
              <line x1={plot.left} x2={plot.right} y1={y} y2={y} stroke="#D9D9D9" />
              <text x={plot.left - 9} y={y + 4} textAnchor="end" fontSize="12">
                {Number(value.toFixed(2))}
              </text>
            </g>
          );
        })}
        {chart.chartType !== 'pie' &&
          (chart.chartType === 'bar' ? (
            <>
              <line x1={zeroX} x2={zeroX} y1={plot.top} y2={plot.bottom} stroke="#777" />
              <line x1={plot.left} x2={plot.right} y1={plot.bottom} y2={plot.bottom} stroke="#777" />
              {categories.map((category, index) => (
                <text key={`${category}:${index}`} x={plot.left - 10} y={yAtCategory(index) + 4} textAnchor="end" fontSize="12">
                  {category}
                </text>
              ))}
            </>
          ) : (
            <>
              <line x1={plot.left} x2={plot.left} y1={plot.top} y2={plot.bottom} stroke="#777" />
              <line x1={plot.left} x2={plot.right} y1={zeroY} y2={zeroY} stroke="#777" />
              {categories.map((category, index) => (
                <text key={`${category}:${index}`} x={xAtCategory(index)} y={plot.bottom + 22} textAnchor="middle" fontSize="12">
                  {category}
                </text>
              ))}
            </>
          ))}
        <ChartMarks chart={chart} colors={colors} plot={plot} categoryCount={categoryCount} xAtCategory={xAtCategory} xAtValue={xAtValue} yAtValue={yAtValue} />
        {legendSeries.map(({ series, seriesIndex, label }, legendIndex) => (
          <g key={`${seriesIndex}:${label}`}>
            <rect x="735" y={plot.top + legendIndex * 22} width="11" height="11" fill={series.color ?? colors[seriesIndex % colors.length]} />
            <text x="752" y={plot.top + legendIndex * 22 + 10} fontSize="12">
              {label}
            </text>
          </g>
        ))}
      </svg>
    </figure>
  );
}

function ChartMarks({
  chart,
  colors,
  plot,
  categoryCount,
  xAtCategory,
  xAtValue,
  yAtValue,
}: {
  chart: PresentationChart;
  colors: string[];
  plot: { left: number; top: number; right: number; bottom: number };
  categoryCount: number;
  xAtCategory: (index: number) => number;
  xAtValue: (value: number) => number;
  yAtValue: (value: number) => number;
}) {
  if (chart.chartType === 'pie') {
    const series = chart.series[0];
    const total = series.values.reduce((sum, value) => sum + Math.max(value, 0), 0) || 1;
    const centerX = (plot.left + plot.right) / 2;
    const centerY = (plot.top + plot.bottom) / 2;
    const radius = Math.min(plot.right - plot.left, plot.bottom - plot.top) * 0.34;
    let angle = -Math.PI / 2;
    return (
      <>
        {series.values.map((value, index) => {
          const end = angle + (Math.max(value, 0) / total) * Math.PI * 2;
          const path = sectorPath(centerX, centerY, radius, angle, end);
          angle = end;
          return <path key={index} d={path} fill={colors[index % colors.length]} stroke="#FFFFFF" />;
        })}
      </>
    );
  }
  if (chart.chartType === 'column' || chart.chartType === 'bar') {
    return (
      <>
        {chart.series.flatMap((series, seriesIndex) =>
          series.values.map((value, index) => {
            const color = series.color ?? colors[seriesIndex % colors.length];
            if (chart.chartType === 'bar') {
              const band = clusteredCategoryBand(index, categoryCount, seriesIndex, chart.series.length, plot.top, plot.bottom);
              const valueSpan = spanFromBaseline(xAtValue(value), xAtValue(0));
              return <rect key={`${seriesIndex}:${index}`} x={valueSpan.start} y={band.start} width={valueSpan.size} height={band.size} fill={color} />;
            }
            const band = clusteredCategoryBand(index, categoryCount, seriesIndex, chart.series.length, plot.left, plot.right);
            const valueSpan = spanFromBaseline(yAtValue(value), yAtValue(0));
            return <rect key={`${seriesIndex}:${index}`} x={band.start} y={valueSpan.start} width={band.size} height={valueSpan.size} fill={color} />;
          }),
        )}
      </>
    );
  }
  return (
    <>
      {chart.series.map((series, index) => (
        <polyline
          key={`${index}:${series.name ?? ''}`}
          points={series.values.map((value, point) => `${xAtCategory(point)},${yAtValue(value)}`).join(' ')}
          fill="none"
          stroke={series.color ?? colors[index % colors.length]}
          strokeWidth="4"
          strokeLinejoin="round"
          strokeLinecap="round"
        />
      ))}
    </>
  );
}

function UnsupportedNode({ node: _node }: { node: Extract<PresentationNode, { type: 'unsupported' }> }) {
  return null;
}

function fillStyle(fill: PresentationFill): CSSProperties {
  if (fill.kind === 'solid') return { backgroundColor: `${fill.color}${alphaHex(1 - (fill.transparency ?? 0))}` };
  if (fill.kind === 'gradient')
    return {
      backgroundImage: `linear-gradient(${fill.angle}deg, ${fill.stops.map(stop => `${stop.color}${alphaHex(1 - (stop.transparency ?? 0))} ${stop.offset * 100}%`).join(', ')})`,
    };
  return {};
}

function alphaHex(value: number): string {
  return Math.round(Math.min(1, Math.max(0, value)) * 255)
    .toString(16)
    .padStart(2, '0');
}

function sectorPath(centerX: number, centerY: number, radius: number, start: number, end: number): string {
  const startX = centerX + radius * Math.cos(start);
  const startY = centerY + radius * Math.sin(start);
  const endX = centerX + radius * Math.cos(end);
  const endY = centerY + radius * Math.sin(end);
  return `M ${centerX} ${centerY} L ${startX} ${startY} A ${radius} ${radius} 0 ${end - start > Math.PI ? 1 : 0} 1 ${endX} ${endY} Z`;
}
