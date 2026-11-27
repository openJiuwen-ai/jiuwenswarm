/**
 * SkillDevAgentRunCard - TEST_RUN 阶段每个测试用例的独立执行卡片
 *
 * 展示单个 (case_name, variant) 的执行轨迹，可折叠。
 */

import { useState } from 'react';
import { StreamingContent } from '../ChatPanel/StreamingContent';
import type { SkillDevMessage } from '../../stores';

interface SkillDevAgentRunCardProps {
  message: SkillDevMessage;
}

const STATUS_ICON: Record<string, string> = {
  running: '○',
  success: '✓',
  error: '✗',
  timeout: '⏱',
};

const STATUS_CLASS: Record<string, string> = {
  running: 'text-accent',
  success: 'text-green-500',
  error: 'text-red-500',
  timeout: 'text-yellow-500',
};

export function SkillDevAgentRunCard({ message }: SkillDevAgentRunCardProps) {
  const [collapsed, setCollapsed] = useState(false);
  const { caseName, caseVariant, caseStatus = 'running', caseOutput = '', casePrompt = '' } = message.metadata ?? {};

  const statusIcon = STATUS_ICON[caseStatus] ?? '○';
  const statusClass = STATUS_CLASS[caseStatus] ?? 'text-text-muted';
  const isRunning = caseStatus === 'running';

  return (
    <div className="my-1.5 rounded-lg border border-border bg-secondary overflow-hidden text-sm">
      {/* Header */}
      <button
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-hover transition-colors"
        onClick={() => setCollapsed((c) => !c)}
      >
        {/* Status icon */}
        <span className={`font-mono text-xs w-4 shrink-0 ${statusClass}`}>
          {isRunning ? (
            <span className="inline-block animate-spin">◌</span>
          ) : (
            statusIcon
          )}
        </span>

        {/* Case name */}
        <span className="font-medium text-text truncate">{caseName}</span>

        {/* Variant badge */}
        {caseVariant === 'with_skill' ? (
          <span className="shrink-0 px-1.5 py-0.5 text-xs rounded bg-accent/10 text-accent border border-accent/20">
            with_skill
          </span>
        ) : (
          <span className="shrink-0 px-1.5 py-0.5 text-xs rounded bg-hover text-text-muted border border-border">
            without_skill
          </span>
        )}

        {/* Expand/collapse chevron */}
        <span className="ml-auto text-text-muted text-xs shrink-0">
          {collapsed ? '▶' : '▼'}
        </span>
      </button>

      {/* Body */}
      {!collapsed && (
        <div className="border-t border-border">
          {/* Query 展示区 */}
          {casePrompt && (
            <div className="px-3 py-2 bg-bg border-b border-border/60">
              <span className="text-xs font-medium text-text-muted uppercase tracking-wide">Query</span>
              <p className="mt-1 text-xs text-text whitespace-pre-wrap break-words">{casePrompt}</p>
            </div>
          )}
          {/* Agent 输出区 */}
          <div className="px-3 pb-3 pt-2">
            {caseOutput ? (
              <StreamingContent content={caseOutput} isStreaming={message.isStreaming === true} />
            ) : (
              <span className="text-text-muted text-xs italic">等待输出…</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
