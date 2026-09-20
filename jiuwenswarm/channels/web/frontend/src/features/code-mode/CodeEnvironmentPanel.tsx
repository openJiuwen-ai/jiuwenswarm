import { FileDiff } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ProjectInfo } from '../../types';
import { CodeBranchSelector } from './CodeBranchSelector';
import { CodeCommitPushControl } from './CodeCommitPushControl';
import type { CodeGitDiffWatchController } from './useCodeGitDiffWatch';

interface CodeEnvironmentPanelProps {
  project: ProjectInfo;
  isProcessing: boolean;
  diffWatch: CodeGitDiffWatchController;
  onReview: () => void;
}

export function CodeEnvironmentPanel({ project, isProcessing, diffWatch, onReview }: CodeEnvironmentPanelProps) {
  const { t } = useTranslation();
  const stats = diffWatch.summary?.current?.stats;
  const loading = diffWatch.summaryLoading && !diffWatch.summary;
  const currentUnavailable = Boolean(diffWatch.summary && !diffWatch.summary.repo.is_git && !diffWatch.summary.current);
  const unavailable = Boolean((diffWatch.summaryError && !diffWatch.summary) || currentUnavailable);
  const repoIsParentOfProject = Boolean(diffWatch.summary?.repo.repo_is_parent_of_project);
  const repoRoot = diffWatch.summary?.repo.repo_root ?? null;

  return (
    <section className="code-environment" aria-label={t('codeMode.environment')} data-testid="code-mode-environment-panel">
      {repoIsParentOfProject ? (
        <div className="code-environment__notice" role="status" data-testid="code-mode-environment-repo-parent-notice">
          当前 Git 仓库位于项目目录的上级（{repoRoot}），分支变更会统计项目目录之外的文件。
        </div>
      ) : null}
      <button type="button" className="code-environment__row" onClick={onReview} title={diffWatch.summaryError || '打开代码审核'} data-testid="code-mode-environment-review-button">
        <FileDiff size={15} />
        <span>{t('codeMode.changes')}</span>
        <small className="code-environment__stats" aria-live="polite" data-testid="code-mode-environment-stats" data-variant={loading ? 'loading' : unavailable ? 'unavailable' : 'ready'}>
          {loading ? (
            '…'
          ) : unavailable ? (
            '—'
          ) : (
            <>
              <b className="code-stat-added">+{stats?.lines_added ?? 0}</b>
              <b className="code-stat-removed">-{stats?.lines_removed ?? 0}</b>
            </>
          )}
        </small>
      </button>
      <div className="code-environment__row code-environment__row--branch">
        <CodeBranchSelector project={project} compact variant="environment" disabled={isProcessing} liveRepo={diffWatch.summary?.repo ?? null} />
      </div>
      <CodeCommitPushControl
        project={project}
        branch={diffWatch.summary?.repo.branch || project.git.branch || null}
        hasChanges={Boolean(diffWatch.summary?.current?.is_dirty)}
        filesChanged={stats?.files_changed ?? 0}
        isGit={Boolean(diffWatch.summary?.repo.is_git)}
        transient={Boolean(diffWatch.summary?.repo.transient)}
        isProcessing={isProcessing}
        variant="environment"
        onSuccess={diffWatch.refresh}
      />
    </section>
  );
}
