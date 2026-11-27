/**
 * SkillDevThinkingBlock - 通用思考块组件
 *
 * 支持两种状态：
 * - type === 'thinking'：实时流式展开，isStreaming 时显示光标/"思考中"标签
 * - type === 'thinking_block'：默认折叠，可点击展开查看完整内容
 */

import { useState, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import type { SkillDevMessage } from '../../stores';
import { formatTimestamp } from '../../utils';
import clsx from 'clsx';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface SkillDevThinkingBlockProps {
  message: SkillDevMessage;
}

export function SkillDevThinkingBlock({ message }: SkillDevThinkingBlockProps) {
  const { t } = useTranslation();
  const { content, timestamp, type, isStreaming } = message;

  // 始终默认折叠，用户手动点击才展开
  const [isCollapsed, setIsCollapsed] = useState(true);
  // 记录用户是否主动操作过折叠/展开，有操作则不再被自动状态覆盖
  const userToggledRef = useRef(false);
  const contentRef = useRef<HTMLDivElement>(null);

  // 当同一消息实例从 thinking 转为 thinking_block 时，若用户未操作过则同步折叠
  useEffect(() => {
    if (type === 'thinking_block' && !userToggledRef.current) {
      setIsCollapsed(true);
    }
  }, [type]);

  // 展开时自动滚动到底部（显示最新内容）
  useEffect(() => {
    if (!isCollapsed && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight;
    }
  }, [isCollapsed]);

  // 流式时自动滚动到底部
  useEffect(() => {
    if (isStreaming && contentRef.current) {
      contentRef.current.scrollTop = contentRef.current.scrollHeight;
    }
  }, [content, isStreaming]);

  return (
    <div className="mb-3 animate-rise">
      <div
        className={clsx(
          'chat-bubble assistant',
          'bg-secondary border border-border'
        )}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between px-3 py-2 border-b border-border">
          <div className="flex items-center gap-2">
            <svg
              className={clsx('w-4 h-4 shrink-0', isStreaming ? 'text-accent animate-pulse' : 'text-accent')}
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.95-.356-1.869-1-2.534l-.548-.547z" />
            </svg>
            <span className="text-sm font-medium text-accent">
              {isStreaming
                ? t('skilldev.thinking.streaming', '思考中...')
                : t('skilldev.thinking.title', '思考过程')}
            </span>
            {isStreaming && <span className="streaming-cursor" />}
          </div>
          {/* 始终显示折叠/展开按钮 */}
          <button
            onClick={() => {
              userToggledRef.current = true;
              setIsCollapsed(!isCollapsed);
            }}
            className="text-xs text-text-muted hover:text-text transition-colors shrink-0"
          >
            {isCollapsed ? t('skilldev.thinking.expand', '展开') : t('skilldev.thinking.collapse', '收起')}
          </button>
        </div>

        {/* 折叠时：标题下方显示单行内容预览 */}
        {isCollapsed && (
          <div className="px-3 py-2 text-sm text-text-muted truncate">
            {content}
          </div>
        )}

        {/* 展开时的内容区 */}
        {!isCollapsed && (
          <div
            ref={contentRef}
            className="px-3 py-2 text-sm text-text max-h-[300px] overflow-y-auto"
          >
            {isStreaming ? (
              <span className="whitespace-pre-wrap">{content}</span>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
            )}
          </div>
        )}
      </div>
      <div className="flex items-center gap-3 text-sm mt-2 text-text-muted">
        <span>{formatTimestamp(timestamp)}</span>
      </div>
    </div>
  );
}
