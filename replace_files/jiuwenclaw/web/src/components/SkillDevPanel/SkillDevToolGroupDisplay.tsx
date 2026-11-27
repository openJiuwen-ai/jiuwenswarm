import { useTranslation } from 'react-i18next';
import clsx from 'clsx';
import type { ToolExecution } from '../../types';
import { formatToolArguments, formatToolResult } from '../../utils';
import { useState } from 'react';

interface SkillDevToolGroupDisplayProps {
  executions: ToolExecution[];
}

function SkillDevToolExecutionItem({ execution }: { execution: ToolExecution }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const { toolCall, result, status } = execution;
  const isSuccess = status === 'completed';
  const isError = status === 'error';
  const isPending = status === 'pending';

  return (
    <div className="tool-pair-item animate-rise" data-testid={`skilldev-tool-execution-${toolCall.id}`}>
      <div className="tool-pair-header" onClick={() => setExpanded((v) => !v)}>
        <span className={clsx('tool-pair-icon', isSuccess ? 'success' : isError ? 'error' : isPending ? 'pending' : 'warning')}>
          {isSuccess ? '✓' : isError ? '!' : '…'}
        </span>
        <span className="tool-pair-name">{toolCall.name}</span>
        {toolCall.formatted_args && (
          <span className="tool-pair-summary">{toolCall.formatted_args}</span>
        )}
        {result && (
          <span className={clsx('tool-pair-result-badge', result.success ? 'success' : 'error')}>
            {result.summary || (result.success ? t('chatUi.toolResult.success') : t('chatUi.toolResult.failed'))}
          </span>
        )}
        <span className="tool-pair-toggle">{expanded ? '▼' : '▶'}</span>
      </div>
      {expanded && (
        <div className="tool-pair-detail">
          {Object.keys(toolCall.arguments || {}).length > 0 && (
            <div className="tool-pair-section">
              <div className="tool-pair-section-label">{t('chatUi.toolResult.arguments')}</div>
              <pre className="tool-pair-pre">{formatToolArguments(toolCall.arguments)}</pre>
            </div>
          )}
          {result && (
            <div className="tool-pair-section">
              <div className="tool-pair-section-label">{t('chatUi.toolResult.result')}</div>
              <pre className={clsx('tool-pair-pre', !result.success && 'error')}>
                {formatToolResult(result.result, 1000)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function SkillDevToolGroupDisplay({ executions }: SkillDevToolGroupDisplayProps) {
  const { t } = useTranslation();
  if (executions.length === 0) {
    return null;
  }

  return (
    <div className="tool-group-container animate-rise" data-testid="skilldev-tool-group">
      <div className="tool-group-header">
        <div className="tool-group-header-left">
          <span>{t('chatUi.toolGroup.executed', { totalPairs: executions.length })}</span>
        </div>
      </div>
      <div className="tool-group-scroll">
        {executions.map((execution) => (
          <SkillDevToolExecutionItem key={execution.toolCallId} execution={execution} />
        ))}
      </div>
    </div>
  );
}
