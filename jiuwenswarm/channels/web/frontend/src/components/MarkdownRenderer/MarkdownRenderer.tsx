import { createContext, useContext, useMemo, type AnchorHTMLAttributes, type HTMLAttributes, type MouseEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeSlug from 'rehype-slug';
import remarkGfm from 'remark-gfm';
import type { Element as HastElement } from 'hast';
import { unescapeLiteralNewlines } from '../../utils/finalContent';
import { getFencedCodeBlock } from './codeBlocks/fencedCode';
import { getFencedCodeAdapter } from './codeBlocks/registry';
import { repairCollapsedGfmTables } from './markdownTransforms';
import './MarkdownRenderer.css';

interface MarkdownRendererProps {
  content: string;
  className?: string;
  testId?: string;
  /** 拦截非锚点链接点击。返回 true 表示已处理（阻止默认导航/新开标签）。 */
  onLinkClick?: (href: string, event: MouseEvent<HTMLAnchorElement>) => boolean | void;
  /**
   * @deprecated 兼容保留（个人上下文图谱详情等调用方仍传入）：MR !4410
   * 迁入后 #锚点 链接默认不开新页，该开关不再影响行为。
   */
  inPageAnchors?: boolean;
}

const MarkdownContentLinesContext = createContext<string[]>([]);

interface MarkdownLinkPolicy {
  onLinkClick?: MarkdownRendererProps['onLinkClick'];
  inPageAnchors: boolean;
}

const MarkdownLinkPolicyContext = createContext<MarkdownLinkPolicy>({ inPageAnchors: false });

function MarkdownLink({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>): JSX.Element {
  const isFragmentLink = href?.startsWith('#');
  const isExternalLink = /^https?:/i.test(href ?? '');
  const { onLinkClick } = useContext(MarkdownLinkPolicyContext);

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
  // 锚点链接不开新页（MR !4410：修复 md 内部跳转打开新浏览器页签的问题）；
  // 内部相对链接：提供 onLinkClick 时交给其拦截（不开新页），未提供时保持
  // 新开标签（与历史行为一致，避免相对链接在当前页导航破坏 SPA）。
  const openInNewTab = isExternalLink || (!isFragmentLink && !onLinkClick);

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

type MarkdownPreProps = HTMLAttributes<HTMLPreElement> & { node?: HastElement };

function MarkdownPre({ children, node, ...props }: MarkdownPreProps): JSX.Element {
  const contentLines = useContext(MarkdownContentLinesContext);
  const codeBlock = getFencedCodeBlock(children, contentLines, node);
  if (codeBlock) {
    const adapter = getFencedCodeAdapter(codeBlock);
    if (adapter) {
      const Renderer = adapter.Renderer;
      return <Renderer code={codeBlock.code} complete={codeBlock.complete} />;
    }
  }

  return <pre {...props}>{children}</pre>;
}

function MarkdownTable({ children, ...props }: HTMLAttributes<HTMLTableElement>): JSX.Element {
  return (
    <div className="chat-markdown-table-wrap">
      <table {...props}>{children}</table>
    </div>
  );
}

const MARKDOWN_COMPONENTS = {
  a: MarkdownLink,
  pre: MarkdownPre,
  table: MarkdownTable,
};

export function MarkdownRenderer({ content, className, testId, onLinkClick, inPageAnchors = false }: MarkdownRendererProps): JSX.Element {
  const markdown = useMemo(() => repairCollapsedGfmTables(unescapeLiteralNewlines(content)), [content]);
  const contentLines = useMemo(() => markdown.split(/\r\n|\n|\r/), [markdown]);

  return (
    <div className={className} data-testid={testId}>
      <MarkdownContentLinesContext.Provider value={contentLines}>
        <MarkdownLinkPolicyContext.Provider value={{ onLinkClick, inPageAnchors }}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSlug]} components={MARKDOWN_COMPONENTS}>
            {markdown}
          </ReactMarkdown>
        </MarkdownLinkPolicyContext.Provider>
      </MarkdownContentLinesContext.Provider>
    </div>
  );
}
