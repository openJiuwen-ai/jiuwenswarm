import { useState } from 'react';
import { ChevronDown, ChevronUp, FileCode2, LoaderCircle, RefreshCw } from 'lucide-react';
import type { CodeReviewTarget, GitTurnChangeAction, GitTurnDiff } from './types';

interface CodeChangesCardProps {
  diff: GitTurnDiff;
  refreshing?: boolean;
  isLatest?: boolean;
  isProcessing?: boolean;
  operation?: GitTurnChangeAction | null;
  operationError?: string | null;
  onRefresh: () => void;
  onReview: (target: CodeReviewTarget) => void;
  onDiscard: () => void;
  onRedo: () => void;
}

export function CodeChangesCard({
  diff,
  refreshing = false,
  isLatest = false,
  isProcessing = false,
  operation = null,
  operationError = null,
  onRefresh,
  onReview,
  onDiscard,
  onRedo,
}: CodeChangesCardProps) {
  const [expanded, setExpanded] = useState(false);
  const files = Object.values(diff.files);
  const visibleFiles = expanded ? files : files.slice(0, 3);
  const reviewTarget: CodeReviewTarget = {
    source: 'last_turn',
    changeSetId: diff.change_set_id,
    turnIndex: diff.turn_index,
  };
  const discarded = diff.status === 'discarded';
  const canChangeTurn = isLatest && (diff.status === 'completed' || discarded);
  const actionLabel = discarded ? '重新应用' : '撤销';
  const actionTitle = isProcessing ? '当前任务执行中，请停止后再操作' : actionLabel;

  if (files.length === 0) return null;

  return (
    <section className={`code-changes-card${discarded ? ' is-discarded' : ''}`} aria-label="已编辑文件">
      <div className="code-changes-card__header">
        <span className="code-changes-card__icon">
          <FileCode2 size={20} />
        </span>
        <div className="code-changes-card__heading">
          <strong>已编辑文件</strong>
          <span>
            <b className="code-stat-added">+{diff.stats.lines_added}</b>
            <b className="code-stat-removed">-{diff.stats.lines_removed}</b>
          </span>
        </div>
        <button type="button" className="code-changes-card__refresh" onClick={onRefresh} disabled={refreshing} title="刷新修改历史">
          <RefreshCw className={refreshing ? 'code-mode-spin' : undefined} size={15} />
        </button>
        {canChangeTurn ? (
          <button
            type="button"
            className="code-changes-card__action"
            onClick={discarded ? onRedo : onDiscard}
            disabled={isProcessing || operation !== null}
            title={actionTitle}
            aria-busy={operation !== null}
          >
            {operation ? <LoaderCircle className="code-mode-spin" size={14} /> : null}
            {operation ? (operation === 'discard' ? '撤销中' : '应用中') : actionLabel}
          </button>
        ) : null}
        <button type="button" className="code-changes-card__review" onClick={() => onReview(reviewTarget)}>
          审核
        </button>
      </div>
      {operationError ? (
        <div className="code-changes-card__error" role="alert">
          {operationError}
        </div>
      ) : null}
      <div className="code-changes-card__files">
        {visibleFiles.map(file => (
          <button type="button" key={file.file_path} onClick={() => onReview(reviewTarget)}>
            <span>{file.file_path}</span>
            <small className="code-stat-added">+{file.lines_added}</small>
            <small className="code-stat-removed">-{file.lines_removed}</small>
          </button>
        ))}
      </div>
      {files.length > 3 ? (
        <button type="button" className="code-changes-card__expand" onClick={() => setExpanded(value => !value)}>
          {expanded ? '收起文件' : `显示全部 ${files.length} 个文件`}
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      ) : null}
    </section>
  );
}
