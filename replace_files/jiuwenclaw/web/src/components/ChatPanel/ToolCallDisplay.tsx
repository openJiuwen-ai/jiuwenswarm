/**
 * ToolCallDisplay 组件
 *
 * 工具调用和结果显示
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ToolCall, ToolResult } from '../../types';
import { formatToolArguments, formatToolResult } from '../../utils';
import clsx from 'clsx';

interface ToolCallDisplayProps {
  toolCall?: ToolCall;
  toolResult?: ToolResult;
}

export function ToolCallDisplay({ toolCall, toolResult }: ToolCallDisplayProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);

  if (toolCall) {
    // session 类型：仅显示 会话任务：【description】，不显示 "session" 名称
    const isSession = toolCall.name === 'session';
    const displayTitle = isSession
      ? (toolCall.formatted_args || '会话任务已完成')
      : (toolCall.description ? `${toolCall.name}: ${toolCall.description}` : toolCall.name);

    // 使用格式化的参数摘要（session 类型时 subtitle 已融入 title，不再重复显示）
    const displaySubtitle = isSession ? '' : (toolCall.formatted_args || '');

    return (
      <div className="chat-tool-card animate-rise">
        <div
          className="cursor-pointer"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <div className="flex items-center gap-2">
            <span className="w-5 h-5 rounded bg-accent-2-subtle text-accent-2 flex items-center justify-center text-sm">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437l1.745-1.437m6.615 8.206L15.75 15.75M4.867 19.125h.008v.008h-.008v-.008z" />
              </svg>
            </span>
            <span className="font-mono text-sm font-medium text-text">{displayTitle}</span>
            <span className="text-text-muted text-sm">
              {isExpanded ? '▼' : '▶'}
            </span>
          </div>
          {displaySubtitle && (
            <div className="mt-1 font-mono text-sm text-text-muted truncate">
              {displaySubtitle}
            </div>
          )}
        </div>
        {isExpanded && (
          <div className="mt-2 p-2 rounded-md bg-card border border-border">
            <pre className="font-mono text-sm text-text overflow-x-auto whitespace-pre-wrap">
              {formatToolArguments(toolCall.arguments)}
            </pre>
          </div>
        )}
      </div>
    );
  }

  if (toolResult) {
    // 使用格式化的摘要或默认显示（session 类型优先用 summary，避免出现 "session 完成"）
    const displaySummary = toolResult.summary
      ? toolResult.summary
      : (toolResult.toolName === 'session'
        ? (toolResult.success ? t('chatUi.toolGroup.sessionCompleted') : t('chatUi.toolGroup.sessionFailed'))
        : `${toolResult.toolName} ${toolResult.success ? t('chatUi.toolResult.success') : t('chatUi.toolResult.failed')}`);

    return (
      <div className="chat-tool-card animate-rise">
        <div
          className="cursor-pointer"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <div className="flex items-center gap-2">
            <span className={clsx(
              'w-5 h-5 rounded flex items-center justify-center text-sm',
              toolResult.success
                ? 'bg-ok-subtle text-ok'
                : 'bg-danger-subtle text-danger'
            )}>
              {toolResult.success ? (
                <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              ) : (
                <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              )}
            </span>
            <span className={clsx(
              'font-mono text-sm',
              toolResult.success ? 'text-text-muted' : 'text-danger'
            )}>
              {displaySummary}
            </span>
            <span className="text-text-muted text-sm ml-auto">
              {isExpanded ? '▼' : '▶'}
            </span>
          </div>
        </div>
        {isExpanded && (
          <div className="mt-2 p-2 rounded-md bg-card border border-border">
            <pre className="font-mono text-sm text-text overflow-x-auto whitespace-pre-wrap max-h-60">
              {formatToolResult(toolResult.result)}
            </pre>
          </div>
        )}
      </div>
    );
  }

  return null;
}
