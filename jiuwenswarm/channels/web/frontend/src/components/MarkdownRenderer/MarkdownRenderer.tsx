import { createContext, useContext, useMemo, type AnchorHTMLAttributes, type HTMLAttributes } from 'react';
import ReactMarkdown from 'react-markdown';
import type { Element as HastElement } from 'hast';
import { unescapeLiteralNewlines } from '../../utils/finalContent';
import { getFencedCodeBlock } from './codeBlocks/fencedCode';
import { getFencedCodeAdapter } from './codeBlocks/registry';
import { MARKDOWN_REHYPE_PLUGINS, MARKDOWN_REMARK_PLUGINS } from './markdownPlugins';
import { repairCollapsedGfmTables } from './markdownTransforms';
import './MarkdownRenderer.css';

interface MarkdownRendererProps {
  content: string;
  className?: string;
  testId?: string;
  isStreaming?: boolean;
}

const MarkdownContentLinesContext = createContext<string[]>([]);
const MarkdownStreamingContext = createContext(false);

function MarkdownLink({ href, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement>): JSX.Element {
  const isFragmentLink = href?.startsWith('#');

  return (
    <a href={href} target={isFragmentLink ? undefined : '_blank'} rel={isFragmentLink ? undefined : 'noopener noreferrer'} {...props}>
      {children}
    </a>
  );
}

type MarkdownPreProps = HTMLAttributes<HTMLPreElement> & { node?: HastElement };

function MarkdownPre({ children, node, ...props }: MarkdownPreProps): JSX.Element {
  const contentLines = useContext(MarkdownContentLinesContext);
  const isStreaming = useContext(MarkdownStreamingContext);
  const codeBlock = getFencedCodeBlock(children, contentLines, node);
  if (codeBlock) {
    const adapter = getFencedCodeAdapter(codeBlock);
    if (adapter) {
      const Renderer = adapter.Renderer;
      return <Renderer code={codeBlock.code} complete={codeBlock.complete} isStreaming={isStreaming} />;
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

export function MarkdownRenderer({ content, className, testId, isStreaming = false }: MarkdownRendererProps): JSX.Element {
  const markdown = useMemo(() => repairCollapsedGfmTables(unescapeLiteralNewlines(content)), [content]);
  const contentLines = useMemo(() => markdown.split(/\r\n|\n|\r/), [markdown]);

  return (
    <div className={className} data-testid={testId}>
      <MarkdownContentLinesContext.Provider value={contentLines}>
        <MarkdownStreamingContext.Provider value={isStreaming}>
          <ReactMarkdown remarkPlugins={MARKDOWN_REMARK_PLUGINS} rehypePlugins={MARKDOWN_REHYPE_PLUGINS} components={MARKDOWN_COMPONENTS}>
            {markdown}
          </ReactMarkdown>
        </MarkdownStreamingContext.Provider>
      </MarkdownContentLinesContext.Provider>
    </div>
  );
}
