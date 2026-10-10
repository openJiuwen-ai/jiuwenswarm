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
  isSymphonyCommandTool,
} from '../../utils/symphonyCommandDisplay';
import { describeToolCall } from './toolCategory';
import { AutoReviewerDetails, AutoReviewerStatusBadge } from './AutoReviewerStatus';

interface ToolCallDisplayProps {
  toolCall?: ToolCall;
  toolResult?: ToolResult;
}

function DisclosureChevron({ open }: { open: boolean }) {
  return (
    <span
      className={clsx('tool-tree-item__disclosure', open && 'is-open')}
      aria-hidden="true"
    >
      <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8">
        <path strokeLinecap="round" strokeLinejoin="round" d="m8 6 4 4-4 4" />
      </svg>
    </span>
  );
}

export function ToolCallDisplay({ toolCall, toolResult }: ToolCallDisplayProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);

  if (toolCall) {
    const isSession = toolCall.name === 'session';
    const displayTitle = isSession
      ? (toolCall.formatted_args || t('chatUi.toolGroup.sessionCompleted'))
      : describeToolCall(toolCall, t);
    const callGoal = toolCall.call_goal?.trim() || '';
    // session：subtitle 融入 title；其余 call_goal 跟在标题同行，formatted_args 仍作下一行副标题
    const displaySubtitle = isSession ? '' : (toolCall.formatted_args || '');

    return (
      <div className="chat-tool-card animate-rise" data-testid="chat-panel-tool-call-card" data-variant="call">
        <div
          className="cursor-pointer"
          data-testid="chat-panel-tool-call-card-header"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <div className="flex items-center gap-2 min-w-0">
            <span className="w-5 h-5 shrink-0 rounded bg-accent-2-subtle text-accent-2 flex items-center justify-center text-sm">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437l1.745-1.437m6.615 8.206L15.75 15.75M4.867 19.125h.008v.008h-.008v-.008z" />
              </svg>
            </span>
            <span className="min-w-0 flex-1 inline-flex items-center gap-1.5 overflow-hidden">
              <span className="font-mono text-sm font-medium text-text shrink-0 max-w-[60%] truncate" data-testid="chat-panel-tool-call-card-title">{displayTitle}</span>
              {callGoal ? (
                <span className="font-mono text-sm text-text-muted truncate min-w-0" data-testid="chat-panel-tool-call-card-goal">
                  {callGoal}
                </span>
              ) : null}
            </span>
            <AutoReviewerStatusBadge reviewer={toolCall.reviewer} />
            <DisclosureChevron open={isExpanded} />
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
            <AutoReviewerDetails reviewer={toolCall.reviewer} />
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
          <div className="flex items-center gap-2 min-w-0">
            <span className={clsx(
              'w-5 h-5 shrink-0 rounded flex items-center justify-center text-sm',
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
              'font-mono text-sm min-w-0 flex-1 truncate',
              toolResult.pending
                ? 'text-text-muted'
                : toolResult.success ? 'text-text-muted' : 'text-danger'
            )} data-testid="chat-panel-tool-call-card-summary">
              {displaySummary}
            </span>
            <AutoReviewerStatusBadge reviewer={toolResult.reviewer} />
            <DisclosureChevron open={isExpanded} />
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
            <AutoReviewerDetails reviewer={toolResult.reviewer} />
          </div>
        )}
      </div>
    );
  }

  return null;
}
