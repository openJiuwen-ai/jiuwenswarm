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
import {
  countResultWords,
  getSymphonyCommandLabel,
  isSymphonyCommandTool,
  parseSymphonyCommandAction,
} from '../../utils/symphonyCommandDisplay';

interface ToolCallDisplayProps {
  toolCall?: ToolCall;
  toolResult?: ToolResult;
}

export function ToolCallDisplay({ toolCall, toolResult }: ToolCallDisplayProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);

  if (toolCall) {
    // 后端 display_name 始终优先；未下发时，session 不显示原始工具名。
    const isSession = toolCall.name === 'session';
    const displayName = toolCall.display_name?.trim();
    const isSymphonyCommand = isSymphonyCommandTool(toolCall.name);
    const symphonyAction = isSymphonyCommand
      ? parseSymphonyCommandAction(toolCall.arguments)
      : null;
    const symphonyTitle = symphonyAction
      ? (() => {
          const label = getSymphonyCommandLabel(symphonyAction);
          return t(label.key, label.values);
        })()
      : t('chatUi.toolGroup.symphony.command');
    const displayTitle = displayName
      || (isSession
        ? (toolCall.formatted_args || '会话任务已完成')
        : isSymphonyCommand
          ? symphonyTitle
          : (toolCall.description ? `${toolCall.name}: ${toolCall.description}` : toolCall.name));

    // 使用格式化的参数摘要（session 类型时 subtitle 已融入 title，不再重复显示）
    const displaySubtitle = isSession ? '' : (toolCall.formatted_args || '');

    return (
      <div className="chat-tool-card animate-rise" data-testid="chat-panel-tool-call-card" data-variant="call">
        <div
          className="cursor-pointer"
          data-testid="chat-panel-tool-call-card-header"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <div className="flex items-center gap-2">
            <span className="w-5 h-5 rounded bg-accent-2-subtle text-accent-2 flex items-center justify-center text-sm">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437l1.745-1.437m6.615 8.206L15.75 15.75M4.867 19.125h.008v.008h-.008v-.008z" />
              </svg>
            </span>
            <span className="font-mono text-sm font-medium text-text" data-testid="chat-panel-tool-call-card-title">{displayTitle}</span>
            <span className="text-text-muted text-sm">
              {isExpanded ? '▼' : '▶'}
            </span>
          </div>
          {displaySubtitle && (
            <div className="mt-1 font-mono text-sm text-text-muted truncate" data-testid="chat-panel-tool-call-card-subtitle">
              {displaySubtitle}
            </div>
          )}
        </div>
        {isExpanded && (
          <div className="mt-2 p-2 rounded-md bg-card border border-border" data-testid="chat-panel-tool-call-card-arguments">
            <pre className="font-mono text-sm text-text overflow-x-auto whitespace-pre-wrap">
              {formatToolArguments(toolCall.arguments)}
            </pre>
          </div>
        )}
      </div>
    );
  }

  if (toolResult) {
    const isSymphonyCommand = isSymphonyCommandTool(toolResult.toolName);
    // 使用格式化的摘要或默认显示（session 类型优先用 summary，避免出现 "session 完成"）
    const displaySummary = toolResult.summary
      ? toolResult.summary
      : (toolResult.toolName === 'session'
        ? (toolResult.success ? t('chatUi.toolGroup.sessionCompleted') : t('chatUi.toolGroup.sessionFailed'))
        : isSymphonyCommand
          ? (toolResult.success
            ? t('chatUi.toolGroup.symphony.completed')
            : t('chatUi.toolGroup.symphony.failed'))
          : `${toolResult.toolName} ${toolResult.success ? t('chatUi.toolResult.success') : t('chatUi.toolResult.failed')}`);
    const resultWordCount = isSymphonyCommand
      ? countResultWords(toolResult.result)
      : null;

    return (
      <div className="chat-tool-card animate-rise" data-testid="chat-panel-tool-call-card" data-variant="result">
        <div
          className="cursor-pointer"
          data-testid="chat-panel-tool-call-card-header"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <div className="flex items-center gap-2">
            <span className={clsx(
              'w-5 h-5 rounded flex items-center justify-center text-sm',
              toolResult.pending
                ? 'bg-card text-text-muted'
                : toolResult.success
                  ? 'bg-ok-subtle text-ok'
                  : 'bg-danger-subtle text-danger'
            )} data-testid="chat-panel-tool-call-card-status-icon" data-variant={toolResult.success ? 'success' : 'failed'}>
              {toolResult.pending ? (
                <span className="text-xs" aria-hidden="true">●</span>
              ) : toolResult.success ? (
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
              toolResult.pending
                ? 'text-text-muted'
                : toolResult.success ? 'text-text-muted' : 'text-danger'
            )} data-testid="chat-panel-tool-call-card-summary">
              {displaySummary}
            </span>
            <span className="text-text-muted text-sm ml-auto">
              {isExpanded ? '▼' : '▶'}
            </span>
          </div>
        </div>
        {isExpanded && (
          <div className="mt-2 p-2 rounded-md bg-card border border-border" data-testid="chat-panel-tool-call-card-result">
            {resultWordCount !== null && (
              <div className="mb-2 flex justify-end">
                <span className="px-2 py-0.5 rounded-full border border-border text-xs text-text-muted">
                  {t('chatUi.toolGroup.symphony.resultWords', {
                    count: resultWordCount,
                  })}
                </span>
              </div>
            )}
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
