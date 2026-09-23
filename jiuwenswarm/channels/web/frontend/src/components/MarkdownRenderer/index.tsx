import { Children, createContext, isValidElement, useContext, useEffect, useId, useMemo, useRef, useState, type AnchorHTMLAttributes, type HTMLAttributes, type MouseEvent, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import type { MermaidConfig } from 'mermaid';
import type { Element as HastElement } from 'hast';
import { getSvgNaturalHeight, getSvgNaturalWidth } from '../../utils/svgDimensions';
import { unescapeLiteralNewlines } from '../../utils/finalContent';
import { Check, Copy, RotateCcw, ZoomIn, ZoomOut } from 'lucide-react';
import './MarkdownRenderer.css';

interface MarkdownRendererProps {
  content: string;
  className?: string;
  testId?: string;
  /** 拦截非锚点链接点击。返回 true 表示已处理（阻止默认导航/新开标签）。 */
  onLinkClick?: (href: string, event: MouseEvent<HTMLAnchorElement>) => boolean | void;
  /**
   * 页内锚点导航开关。默认关闭——#锚点 与其他链接一致新开标签页（历史基线，
   * 企业版聊天等共用方不受影响）；个人上下文图谱详情等页内场景显式开启。
   */
  inPageAnchors?: boolean;
}

interface MarkdownLinkPolicy {
  onLinkClick?: MarkdownRendererProps['onLinkClick'];
  inPageAnchors: boolean;
}

const MarkdownLinkPolicyContext = createContext<MarkdownLinkPolicy>({ inPageAnchors: false });

type MermaidRenderState = { status: 'loading'; svg: '' } | { status: 'rendered'; svg: string } | { status: 'error'; svg: '' };

const MERMAID_CONFIG: MermaidConfig = {
  startOnLoad: false,
  suppressErrorRendering: true,
  securityLevel: 'strict',
  htmlLabels: false,
  flowchart: { useMaxWidth: false },
  theme: 'default',
};

function ToolbarButton({ title, onClick, children }: { title: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type='button' title={title} onClick={onClick} className='markdown-toolbar-btn'>
      {children}
    </button>
  );
}

function TogglePill({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type='button' onClick={onClick} className={clsx('markdown-toggle-pill', active && 'markdown-toggle-pill--active')}>
      {children}
    </button>
  );
}

function clampScale(scale: number): number {
  return Math.min(Math.max(scale, 0.25), 3);
}

const MERMAID_CANVAS_MAX_HEIGHT = 600;
const MERMAID_CANVAS_TOP_OFFSET = 24;
const MERMAID_CANVAS_BOTTOM_OFFSET = 24;

function MermaidBlock({ code }: { code: string }) {
  const { t } = useTranslation();
  const diagramId = `mermaid-${useId().replace(/[^A-Za-z0-9_-]/g, '_')}`;
  const [renderState, setRenderState] = useState<MermaidRenderState>({
    status: 'loading',
    svg: '',
  });
  const [viewMode, setViewMode] = useState<'image' | 'code'>('image');
  const [scale, setScale] = useState(1);
  const [fitScale, setFitScale] = useState(1);
  const [copied, setCopied] = useState(false);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [isDragging, setIsDragging] = useState(false);
  const [canvasHeight, setCanvasHeight] = useState(MERMAID_CANVAS_MAX_HEIGHT);
  const [alignTop, setAlignTop] = useState(false);
  const isDraggingRef = useRef(false);
  const dragStartRef = useRef({ x: 0, y: 0 });
  const panStartRef = useRef({ x: 0, y: 0 });
  const canvasRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    async function render(): Promise<void> {
      setRenderState({ status: 'loading', svg: '' });
      try {
        const mermaid = (await import('mermaid')).default;
        mermaid.initialize(MERMAID_CONFIG);
        const { svg } = await mermaid.render(diagramId, code);
        if (!cancelled) setRenderState({ status: 'rendered', svg });
      } catch {
        if (!cancelled) setRenderState({ status: 'error', svg: '' });
      }
    }
    render();
    return () => {
      cancelled = true;
    };
  }, [code, diagramId]);

  useEffect(() => {
    if (renderState.status !== 'rendered' || viewMode !== 'image') return;
    const svg = canvasRef.current?.querySelector('svg');
    if (!svg) return;

    const updateDimensions = () => {
      const naturalHeight = getSvgNaturalHeight(svg);
      const naturalWidth = getSvgNaturalWidth(svg);
      if (naturalHeight <= 0) return;

      const containerWidth = canvasRef.current?.clientWidth ?? 0;
      const availableHeight = MERMAID_CANVAS_MAX_HEIGHT - MERMAID_CANVAS_TOP_OFFSET - MERMAID_CANVAS_BOTTOM_OFFSET;

      const scaleToFitWidth = containerWidth > 0 && naturalWidth > 0 ? containerWidth / naturalWidth : 1;
      const scaleToFitHeight = naturalHeight > 0 ? availableHeight / naturalHeight : 1;
      const nextFitScale = clampScale(Math.min(1, scaleToFitWidth, scaleToFitHeight));

      const scaledHeight = naturalHeight * nextFitScale;
      const contentHeight = scaledHeight + MERMAID_CANVAS_TOP_OFFSET + MERMAID_CANVAS_BOTTOM_OFFSET;
      const nextCanvasHeight = Math.min(MERMAID_CANVAS_MAX_HEIGHT, contentHeight);

      setFitScale(nextFitScale);
      setScale(nextFitScale);
      setPan({ x: 0, y: 0 });
      setCanvasHeight(nextCanvasHeight);
      setAlignTop(contentHeight > MERMAID_CANVAS_MAX_HEIGHT);
    };

    updateDimensions();
    const canvas = canvasRef.current;
    if (!canvas) return;

    const observer = new ResizeObserver(updateDimensions);
    observer.observe(canvas);
    return () => observer.disconnect();
  }, [renderState.status, renderState.svg, viewMode]);

  async function handleCopy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied by the browser.
    }
  }

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
      <pre className='mermaid-error' data-mermaid-status='error'>
        <code>{code}</code>
      </pre>
    );
  }

  if (renderState.status === 'loading') {
    return (
      <pre className='mermaid-loading' data-mermaid-status='loading'>
        <code>{code}</code>
      </pre>
    );
  }

  const panTransform = `translate(${pan.x}px, ${pan.y}px)`;
  const wrapperStyle = alignTop
    ? {
        top: MERMAID_CANVAS_TOP_OFFSET,
        transformOrigin: 'top center' as const,
        transform: `translate(-50%, 0) ${panTransform} scale(${scale})`,
      }
    : {
        top: '50%' as const,
        transformOrigin: 'center center' as const,
        transform: `translate(-50%, -50%) ${panTransform} scale(${scale})`,
      };

  return (
    <div className='mermaid-diagram' data-mermaid-status='rendered'>
      <div className='mermaid-diagram__toolbar'>
        <div className='mermaid-diagram__view-toggle'>
          <TogglePill active={viewMode === 'image'} onClick={() => setViewMode('image')}>
            {t('mermaid.image')}
          </TogglePill>
          <TogglePill active={viewMode === 'code'} onClick={() => setViewMode('code')}>
            {t('mermaid.code')}
          </TogglePill>
        </div>

        <div className='mermaid-diagram__actions'>
          <ToolbarButton title={t('mermaid.copyCode')} onClick={handleCopy}>
            {copied ? <Check size={15} className='text-ok' /> : <Copy size={15} />}
          </ToolbarButton>
          {viewMode === 'image' && (
            <>
              <div className='mermaid-diagram__toolbar-divider' />
              <ToolbarButton title={t('mermaid.zoomIn')} onClick={() => setScale(currentScale => clampScale(currentScale + 0.25))}>
                <ZoomIn size={15} />
              </ToolbarButton>
              <ToolbarButton title={t('mermaid.zoomOut')} onClick={() => setScale(currentScale => clampScale(currentScale - 0.25))}>
                <ZoomOut size={15} />
              </ToolbarButton>
              <ToolbarButton
                title={t('mermaid.fitView')}
                onClick={() => {
                  setScale(fitScale);
                  setPan({ x: 0, y: 0 });
                }}
              >
                <RotateCcw size={15} />
              </ToolbarButton>
            </>
          )}
        </div>
      </div>

      {viewMode === 'image' ? (
        <div
          ref={canvasRef}
          className={clsx('mermaid-canvas', isDragging && 'mermaid-canvas--dragging')}
          style={{ height: canvasHeight }}
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
          <div className='mermaid-svg-wrapper' style={wrapperStyle} dangerouslySetInnerHTML={{ __html: renderState.svg }} />
        </div>
      ) : (
        <div className='mermaid-code-view'>
          <pre>
            <code>{code}</code>
          </pre>
        </div>
      )}
    </div>
  );
}

function getMermaidCode(children: ReactNode): string | null {
  const childArray = Children.toArray(children);
  if (childArray.length !== 1) {
    return null;
  }

  const child = childArray[0];
  if (!isValidElement<HTMLAttributes<HTMLElement>>(child) || child.type !== 'code') {
    return null;
  }

  const className = child.props.className || '';
  if (!/(^|\s)language-mermaid(\s|$)/.test(className)) {
    return null;
  }

  return String(child.props.children).replace(/\n$/, '');
}

function isCompleteCodeFence(contentLines: string[], node?: HastElement): boolean {
  const startLine = node?.position?.start?.line;
  const endLine = node?.position?.end?.line;
  if (!startLine || !endLine) {
    return false;
  }

  const opener = contentLines[startLine - 1];
  const closer = contentLines[endLine - 1];
  if (!opener || !closer) {
    return false;
  }

  const openMatch = /^( {0,3})(`{3,}|~{3,})/.exec(opener);
  if (!openMatch) {
    return false;
  }

  const fenceChar = openMatch[2][0];
  const fenceLen = openMatch[2].length;
  const closePattern = new RegExp(`^ {0,3}\\${fenceChar}{${fenceLen},}\\s*$`);
  return closePattern.test(closer);
}

function MarkdownLink({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>) {
  const isFragmentLink = href?.startsWith('#');
  const isExternalLink = /^https?:/i.test(href ?? '');
  const { onLinkClick, inPageAnchors } = useContext(MarkdownLinkPolicyContext);

  const handleClick = (event: MouseEvent<HTMLAnchorElement>) => {
    if (!href || isFragmentLink || isExternalLink || !onLinkClick) {
      props.onClick?.(event);
      return;
    }
    const handled = onLinkClick(href, event);
    if (handled !== false) {
      event.preventDefault();
    }
  };

  // http(s) 链接始终新开标签页（target=_blank），不论是否提供 onLinkClick；
  // 锚点链接默认与其他链接一致新开页（历史基线），仅在调用方显式开启 inPageAnchors
  // （个人上下文图谱详情等页内场景）时不开新页；内部相对链接：提供 onLinkClick 时
  // 交给其拦截（不开新页），未提供时保持新开标签（与历史行为一致，避免相对链接
  // 在当前页导航破坏 SPA）。
  const openInNewTab = isExternalLink || (!isFragmentLink || !inPageAnchors) && !onLinkClick;

  return (
    <a
      href={href}
      target={openInNewTab ? '_blank' : undefined}
      rel={openInNewTab ? 'noopener noreferrer' : undefined}
      onClick={handleClick}
      {...props}
    >
      {children}
    </a>
  );
}

function MarkdownPre({
  children,
  node,
  contentLines,
  ...props
}: HTMLAttributes<HTMLPreElement> & {
  node?: HastElement;
  contentLines: string[];
}) {
  const code = getMermaidCode(children);
  if (code !== null && isCompleteCodeFence(contentLines, node)) {
    return <MermaidBlock code={code} />;
  }

  return <pre {...props}>{children}</pre>;
}

function MarkdownTable({ children, ...props }: HTMLAttributes<HTMLTableElement>) {
  return (
    <div className='chat-markdown-table-wrap'>
      <table {...props}>{children}</table>
    </div>
  );
}

/**
 * 修复被压成一行的 GFM 表格：行与行之间只剩 ` | |`，remark-gfm 无法识别。
 * 已是多行标准表格时原样返回。
 */
export function repairCollapsedGfmTables(content: string): string {
  if (!content.includes('|') || !/\|[-: ]{3,}/.test(content)) {
    return content;
  }

  const lines = content.split(/\r?\n/);
  const tableLines = lines.filter((line) => /^\s*\|.*\|\s*$/.test(line));
  if (tableLines.length >= 3) {
    return content;
  }

  let next = content;
  // 「正文|页码」标题与表头粘连（左侧须为正文文字，避免拆开 |---| 分隔行）
  next = next.replace(/([\u4e00-\u9fffA-Za-z0-9*])\|(?=\s*[^\s|\-:])/g, '$1\n\n|');
  // 行与行之间的 `| |` / `||` → 换行（分隔行内部是 `|---`，不会命中）
  next = next.replace(/\|\s*(?=\|)/g, '|\n');
  return next;
}

export function MarkdownRenderer({ content, className, testId, onLinkClick, inPageAnchors = false }: MarkdownRendererProps) {
  const markdown = useMemo(
    () => repairCollapsedGfmTables(unescapeLiteralNewlines(content)),
    [content]
  );
  const contentLines = useMemo(() => markdown.split(/\r\n|\n|\r/), [markdown]);

  const components = useMemo(
    () => ({
      a: MarkdownLink,
      pre: (props: HTMLAttributes<HTMLPreElement> & { node?: HastElement }) => <MarkdownPre {...props} contentLines={contentLines} />,
      table: MarkdownTable,
    }),
    [contentLines]
  );

  return (
    <div className={className} data-testid={testId}>
      <MarkdownLinkPolicyContext.Provider value={{ onLinkClick, inPageAnchors }}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
          {markdown}
        </ReactMarkdown>
      </MarkdownLinkPolicyContext.Provider>
    </div>
  );
}
