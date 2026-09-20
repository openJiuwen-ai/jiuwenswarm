import { useState } from 'react';
import { ChevronDown, ChevronUp, LoaderCircle, RefreshCw } from 'lucide-react';
import { resolveFileIconType } from '../../components/FileIcon';
import folderIcon from '../../assets/file-icons/folder.svg';
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

  const primaryFileType = resolveFileIconType(files[0].file_path);

  return (
    <section className={`code-changes-card${discarded ? ' is-discarded' : ''}`} aria-label="已编辑文件" data-testid="code-mode-changes-card" data-variant={discarded ? 'discarded' : 'active'}>
      <div className="code-changes-card__header" data-testid="code-mode-changes-card-header">
        <span className="code-changes-card__icon" data-variant={primaryFileType}>
          <img src={folderIcon} width={24} height={24} alt="" aria-hidden="true" />
        </span>
        <div className="code-changes-card__heading" data-testid="code-mode-changes-card-heading">
          <strong data-testid="code-mode-changes-card-title">已编辑文件</strong>
          <span>
            <b className="code-stat-added" data-testid="code-mode-changes-card-lines-added">+{diff.stats.lines_added}</b>
            <b className="code-stat-removed" data-testid="code-mode-changes-card-lines-removed">-{diff.stats.lines_removed}</b>
          </span>
        </div>
        <button type="button" className="code-changes-card__refresh" onClick={onRefresh} disabled={refreshing} title="刷新修改历史" data-testid="code-mode-changes-card-refresh">
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
            data-testid="code-mode-changes-card-action"
            data-variant={discarded ? 'redo' : 'discard'}
          >
            {operation ? <LoaderCircle className="code-mode-spin" size={14} /> : null}
            {operation ? (operation === 'discard' ? '撤销中' : '应用中') : actionLabel}
          </button>
        ) : null}
        <button type="button" className="code-changes-card__review" onClick={() => onReview(reviewTarget)} data-testid="code-mode-changes-card-review">
          审核
        </button>
      </div>
      {operationError ? (
        <div className="code-changes-card__error" role="alert" data-testid="code-mode-changes-card-error">
          {operationError}
        </div>
      ) : null}
      <div className="code-changes-card__files" data-testid="code-mode-changes-card-files">
        {visibleFiles.map(file => (
          <button type="button" key={file.file_path} onClick={() => onReview(reviewTarget)} data-testid="code-mode-changes-card-file" data-variant={file.file_path}>
            <span>{file.file_path}</span>
            <small className="code-stat-added">+{file.lines_added}</small>
            <small className="code-stat-removed">-{file.lines_removed}</small>
          </button>
        ))}
      </div>
      {files.length > 3 ? (
        <button type="button" className="code-changes-card__expand" onClick={() => setExpanded(value => !value)} data-testid="code-mode-changes-card-expand">
          {expanded ? '收起文件' : `显示全部 ${files.length} 个文件`}
          {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
        </button>
      ) : null}
    </section>
  );
}
