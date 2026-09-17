import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type HTMLAttributes,
} from 'react';
import { Check, Copy } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { copyToClipboard } from '../../utils/copyToClipboard';

/**
 * 由 MarkdownPre 在其子树内开启：fenced code 的 `<code>` 必须保持原生 DOM
 * （fencedCode.ts 依赖该形状提取语言与内容，mermaid/svg 适配器同样依赖），
 * 只有行内 code 才挂复制入口。
 */
export const InsideFencedCodeContext = createContext(false);

type MarkdownCodeProps = HTMLAttributes<HTMLElement> & { node?: unknown };

type InlineCopyState = 'idle' | 'copied' | 'failed';

/**
 * 行内 code 组件：hover 显示一键复制，复制 AST 原始字符串（界面可能视觉
 * 截断，但复制的是完整原文），成功/失败各自反馈。失败链路与 #4353 的
 * copyToClipboard 语义一致。
 */
export function MarkdownInlineCode({ children, className, node: _node, ...props }: MarkdownCodeProps): JSX.Element {
  const insideFencedCode = useContext(InsideFencedCodeContext);
  const { t } = useTranslation();
  const [copyState, setCopyState] = useState<InlineCopyState>('idle');
  const resetTimerRef = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(resetTimerRef.current), []);

  // 行内 code 的 children 是纯文本；fenced code 或意外结构一律走原生渲染。
  const raw = !insideFencedCode && typeof children === 'string' && children !== '' ? children : null;

  const handleCopy = useCallback(() => {
    if (raw === null) return;
    void (async () => {
      const copied = await copyToClipboard(raw);
      setCopyState(copied ? 'copied' : 'failed');
      if (resetTimerRef.current !== undefined) window.clearTimeout(resetTimerRef.current);
      resetTimerRef.current = window.setTimeout(() => setCopyState('idle'), 1500);
    })();
  }, [raw]);

  if (raw === null) {
    return (
      <code className={className} {...props}>
        {children}
      </code>
    );
  }

  const label =
    copyState === 'copied'
      ? t('markdown.copiedInlineCode')
      : copyState === 'failed'
        ? t('markdown.copyInlineCodeFailed')
        : t('markdown.copyInlineCode');

  return (
    <code
      className={className ? `markdown-inline-code ${className}` : 'markdown-inline-code'}
      {...props}
      data-testid="markdown-inline-code"
      data-state={copyState}
    >
      {children}
      <span
        role="button"
        tabIndex={0}
        data-testid="markdown-inline-code-copy"
        data-state={copyState}
        aria-label={label}
        title={label}
        className="markdown-inline-code__copy"
        onClick={(event) => {
          // 行内 code 可能位于 <a> 内：复制点击不得触发导航或冒泡。
          event.preventDefault();
          event.stopPropagation();
          handleCopy();
        }}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' && event.key !== ' ') return;
          event.preventDefault();
          event.stopPropagation();
          handleCopy();
        }}
      >
        {copyState === 'copied' ? (
          <Check className="markdown-inline-code__icon" strokeWidth={1.5} aria-hidden="true" />
        ) : (
          <Copy className="markdown-inline-code__icon" strokeWidth={1.5} aria-hidden="true" />
        )}
      </span>
    </code>
  );
}
