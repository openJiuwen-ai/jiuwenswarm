import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, LoaderCircle, Plus, Upload, X } from 'lucide-react';
import type { ProjectInfo } from '../../types';
import { gitClient } from './gitClient';
import { defaultCommitMessage, gitPublishErrorMessage, remoteNames, type GitPublishOperation } from './gitPublishState';
import type { GitRepoStatus } from './types';
import './CodeMode.css';

interface CodeCommitPushControlProps {
  project: ProjectInfo;
  branch: string | null;
  hasChanges: boolean;
  filesChanged: number;
  isGit: boolean;
  transient: boolean;
  isProcessing: boolean;
  variant: 'environment' | 'review';
  onSuccess: () => void | Promise<void>;
}

function operationIncludesCommit(operation: GitPublishOperation): boolean {
  return operation === 'commit' || operation === 'commit_push';
}

function operationIncludesPush(operation: GitPublishOperation): boolean {
  return operation === 'push' || operation === 'commit_push';
}

export function CodeCommitPushControl({
  project,
  branch,
  hasChanges,
  filesChanged,
  isGit,
  transient,
  isProcessing,
  variant,
  onSuccess,
}: CodeCommitPushControlProps) {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<GitRepoStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [creatingBranch, setCreatingBranch] = useState(false);
  const [operation, setOperation] = useState<GitPublishOperation>(hasChanges ? 'commit_push' : 'push');
  const [message, setMessage] = useState('');
  const [selectedBranch, setSelectedBranch] = useState(branch ?? '');
  const [remote, setRemote] = useState('origin');
  const [includeUnstaged, setIncludeUnstaged] = useState(true);
  const [setUpstream, setSetUpstream] = useState(false);
  const [branchCreateOpen, setBranchCreateOpen] = useState(false);
  const [branchDraft, setBranchDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  useEffect(() => {
    if (!open) return;
    let disposed = false;
    setLoading(true);
    setStatus(null);
    setError(null);
    setMessage('');
    setIncludeUnstaged(true);
    setBranchCreateOpen(false);
    setBranchDraft('');
    void gitClient
      .status(project.project_id)
      .then(nextStatus => {
        if (disposed) return;
        const currentBranch = nextStatus.repo.branch || '';
        setStatus(nextStatus);
        setSelectedBranch(currentBranch);
        setRemote(remoteNames(nextStatus.branches.remotes)[0]);
        setSetUpstream(!nextStatus.repo.upstream);
        setOperation(nextStatus.working_tree.is_dirty ? (nextStatus.repo.detached ? 'commit' : 'commit_push') : 'push');
      })
      .catch(nextError => {
        if (!disposed) setError(gitPublishErrorMessage(nextError, '加载 Git 状态失败，请重试。'));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [open, project.project_id]);

  useEffect(() => {
    if (!open) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || submitting || creatingBranch) return;
      if (branchCreateOpen) {
        setBranchCreateOpen(false);
        setBranchDraft('');
      } else {
        setOpen(false);
      }
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [branchCreateOpen, creatingBranch, open, submitting]);

  const repositoryHasChanges = status?.working_tree.is_dirty ?? hasChanges;
  const currentBranch = status?.repo.branch || branch || '';
  const localBranches = useMemo(() => {
    const branches = status?.branches.locals ?? [];
    return [...new Set([currentBranch, ...branches].filter(Boolean))];
  }, [currentBranch, status?.branches.locals]);
  const remotes = useMemo(() => remoteNames(status?.branches.remotes ?? []), [status?.branches.remotes]);
  const includesCommit = operationIncludesCommit(operation);
  const includesPush = operationIncludesPush(operation);
  const resolvedCommitMessage = message.trim() || defaultCommitMessage(filesChanged);
  const isUnbornHead = Boolean(status?.repo.is_git && currentBranch && !status.repo.head);
  const triggerDisabled = isProcessing || !isGit || transient;
  const disabledReason = isProcessing
    ? '当前任务执行中，请停止后再操作'
    : !isGit
      ? '当前项目不是 Git 仓库'
      : transient
        ? '仓库正在执行其他 Git 操作'
        : '提交或推送';
  const canSubmit = Boolean(
    status && !loading && !submitting && !creatingBranch && (!includesCommit || repositoryHasChanges) && (!includesPush || (selectedBranch && remote.trim())),
  );

  const createBranch = async () => {
    const nextBranch = branchDraft.trim();
    if (!status || !nextBranch || creatingBranch || submitting) return;
    setCreatingBranch(true);
    setError(null);
    try {
      const result = await gitClient.createBranch(project.project_id, nextBranch);
      setStatus(result.status);
      setSelectedBranch(result.branch);
      setSetUpstream(!result.status.repo.upstream);
      setBranchCreateOpen(false);
      setBranchDraft('');
      void Promise.resolve(onSuccess()).catch(() => undefined);
    } catch (nextError) {
      setError(gitPublishErrorMessage(nextError, '创建分支失败，请重试。'));
    } finally {
      setCreatingBranch(false);
    }
  };

  const submit = async () => {
    if (!status || !canSubmit) return;
    setSubmitting(true);
    setError(null);
    let committedHash: string | null = null;
    let phase: 'commit' | 'push' = includesCommit ? 'commit' : 'push';
    try {
      let pushBranch = selectedBranch;
      if (includesCommit) {
        const commitResult = await gitClient.commit(project.project_id, resolvedCommitMessage, {
          stageAll: includeUnstaged,
        });
        committedHash = commitResult.commit_hash;
        pushBranch = commitResult.status.repo.branch || pushBranch;
      }
      if (includesPush) {
        phase = 'push';
        await gitClient.push(project.project_id, {
          remote: remote.trim(),
          branch: pushBranch,
          setUpstream,
        });
      }

      void Promise.resolve(onSuccess()).catch(() => undefined);
      setOpen(false);
      const hashSuffix = committedHash ? `（${committedHash}）` : '';
      setNotice(operation === 'commit' ? `提交成功${hashSuffix}` : operation === 'push' ? '推送成功' : `提交并推送成功${hashSuffix}`);
    } catch (nextError) {
      const detail = gitPublishErrorMessage(nextError, phase === 'commit' ? '提交失败，请重试。' : '推送失败，请重试。');
      if (committedHash && phase === 'push') {
        void Promise.resolve(onSuccess()).catch(() => undefined);
        setOperation('push');
        setError(`提交已成功（${committedHash}），但推送失败：${detail}`);
      } else {
        setError(detail);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      {variant === 'environment' ? (
        <button
          type="button"
          className="code-environment__row code-environment__row--publish"
          onClick={() => setOpen(true)}
          disabled={triggerDisabled}
          title={disabledReason}
          data-testid="code-mode-publish-trigger"
          data-variant="environment"
        >
          <Upload size={15} />
          <span>提交或推送</span>
        </button>
      ) : (
        <button type="button" className="code-review__publish-button" onClick={() => setOpen(true)} disabled={triggerDisabled} title={disabledReason} data-testid="code-mode-publish-trigger" data-variant="review">
          提交或推送
        </button>
      )}

      {notice
        ? createPortal(
            <div className="code-publish-toast" role="status" aria-live="polite" data-testid="code-mode-publish-toast">
              <CheckCircle2 size={17} aria-hidden="true" />
              <span>{notice}</span>
            </div>,
            document.body,
          )
        : null}

      {open
        ? createPortal(
            <div
              className="code-publish-backdrop"
              role="presentation"
              onMouseDown={event => event.target === event.currentTarget && !submitting && setOpen(false)}
              data-testid="code-mode-publish-dialog-backdrop"
            >
              <form
                className="code-publish-dialog"
                role="dialog"
                aria-modal="true"
                aria-labelledby="code-publish-title"
                onSubmit={event => {
                  event.preventDefault();
                  void submit();
                }}
                data-testid="code-mode-publish-dialog"
              >
                <header className="code-publish-dialog__header" data-testid="code-mode-publish-dialog-header">
                  <h3 id="code-publish-title" data-testid="code-mode-publish-dialog-title">提交或推送</h3>
                  <button type="button" onClick={() => setOpen(false)} disabled={submitting || creatingBranch} aria-label="关闭" data-testid="code-mode-publish-dialog-close">
                    <X size={18} />
                  </button>
                </header>

                {loading ? (
                  <div className="code-publish-dialog__loading" data-testid="code-mode-publish-dialog-loading">
                    <LoaderCircle className="code-mode-spin" size={18} />
                    <span>正在加载 Git 状态…</span>
                  </div>
                ) : (
                  <div className="code-publish-dialog__fields" data-testid="code-mode-publish-dialog-fields">
                    <div className="code-publish-field" data-testid="code-mode-publish-field-branch">
                      <span>目标分支</span>
                      <div className="code-publish-branch-picker">
                        <select
                          value={selectedBranch}
                          onChange={event => {
                            setSelectedBranch(event.target.value);
                            if (event.target.value !== currentBranch) setSetUpstream(true);
                          }}
                          disabled={submitting || creatingBranch || includesCommit}
                          aria-label="目标分支"
                          data-testid="code-mode-publish-field-branch-select"
                        >
                          {localBranches.map(localBranch => (
                            <option key={localBranch} value={localBranch} data-testid="code-mode-publish-field-branch-option" data-variant={localBranch}>
                              {localBranch}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="code-publish-branch-create-trigger"
                          onClick={() => {
                            setBranchCreateOpen(true);
                            setError(null);
                          }}
                          disabled={submitting || creatingBranch || Boolean(status?.repo.transient) || isUnbornHead}
                          title={isUnbornHead ? '空仓库需要完成首次提交后才能创建其他分支' : '创建并检出新分支'}
                          data-testid="code-mode-publish-branch-create-trigger"
                        >
                          <Plus size={15} />
                          <span>新建分支</span>
                        </button>
                      </div>
                      {branchCreateOpen ? (
                        <div className="code-publish-branch-create" data-testid="code-mode-publish-branch-create">
                          <input
                            value={branchDraft}
                            onChange={event => setBranchDraft(event.target.value)}
                            onKeyDown={event => {
                              if (event.key === 'Enter') {
                                event.preventDefault();
                                void createBranch();
                              }
                            }}
                            placeholder="例如：feature/code-mode"
                            maxLength={255}
                            disabled={creatingBranch || submitting}
                            aria-label="新分支名称"
                            autoFocus
                            data-testid="code-mode-publish-branch-create-input"
                          />
                          <button
                            type="button"
                            className="code-mode-button"
                            onClick={() => {
                              setBranchCreateOpen(false);
                              setBranchDraft('');
                            }}
                            disabled={creatingBranch}
                            data-testid="code-mode-publish-branch-create-cancel"
                          >
                            取消
                          </button>
                          <button
                            type="button"
                            className="code-mode-button code-mode-button--primary"
                            onClick={() => void createBranch()}
                            disabled={!branchDraft.trim() || creatingBranch || submitting}
                            data-testid="code-mode-publish-branch-create-submit"
                          >
                            {creatingBranch ? <LoaderCircle className="code-mode-spin" size={14} /> : null}
                            {creatingBranch ? '创建中' : '创建'}
                          </button>
                        </div>
                      ) : null}
                    </div>

                    {includesPush ? (
                      <label className="code-publish-field" data-testid="code-mode-publish-field-remote">
                        <span>远程仓库</span>
                        <select value={remote} onChange={event => setRemote(event.target.value)} disabled={submitting} data-testid="code-mode-publish-field-remote-select">
                          {remotes.map(remoteName => (
                            <option key={remoteName} value={remoteName} data-testid="code-mode-publish-field-remote-option" data-variant={remoteName}>
                              {remoteName}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : null}

                    {includesCommit ? (
                      <label className="code-publish-field code-publish-field--message" data-testid="code-mode-publish-field-message">
                        <span>提交信息</span>
                        <textarea
                          value={message}
                          onChange={event => setMessage(event.target.value)}
                          placeholder={`留空将自动使用：${defaultCommitMessage(filesChanged)}`}
                          maxLength={200}
                          disabled={submitting}
                          data-testid="code-mode-publish-field-message-input"
                        />
                        <small data-testid="code-mode-publish-field-message-count">{message.length}/200</small>
                      </label>
                    ) : null}

                    <fieldset className="code-publish-operations" data-testid="code-mode-publish-operations">
                      <legend data-testid="code-mode-publish-operations-legend">操作类型</legend>
                      {(
                        [
                          ['commit', '提交'],
                          ['commit_push', '提交并推送'],
                          ['push', '推送'],
                        ] as const
                      ).map(([value, label]) => (
                        <label key={value} data-testid="code-mode-publish-operation" data-variant={value}>
                          <input
                            type="radio"
                            name="git-publish-operation"
                            value={value}
                            checked={operation === value}
                            onChange={() => setOperation(value)}
                            disabled={submitting || (value !== 'push' && !repositoryHasChanges)}
                            data-testid="code-mode-publish-operation-input"
                            data-variant={value}
                          />
                          <span>{label}</span>
                        </label>
                      ))}
                    </fieldset>

                    <div className="code-publish-options" data-testid="code-mode-publish-options">
                      {includesCommit ? (
                        <label data-testid="code-mode-publish-option-include-unstaged">
                          <input type="checkbox" checked={includeUnstaged} onChange={event => setIncludeUnstaged(event.target.checked)} disabled={submitting} />
                          <span>包含未暂存的更改</span>
                        </label>
                      ) : null}
                      {includesPush ? (
                        <label data-testid="code-mode-publish-option-set-upstream">
                          <input type="checkbox" checked={setUpstream} onChange={event => setSetUpstream(event.target.checked)} disabled={submitting} />
                          <span>设置为上游分支</span>
                        </label>
                      ) : null}
                    </div>
                  </div>
                )}

                {error ? (
                  <div className="code-publish-dialog__error" role="alert" data-testid="code-mode-publish-dialog-error">
                    {error}
                  </div>
                ) : null}

                <footer className="code-publish-dialog__actions" data-testid="code-mode-publish-dialog-actions">
                  <button type="button" className="code-mode-button" onClick={() => setOpen(false)} disabled={submitting || creatingBranch} data-testid="code-mode-publish-dialog-cancel">
                    取消
                  </button>
                  <button type="submit" className="code-mode-button code-mode-button--primary" disabled={!canSubmit} data-testid="code-mode-publish-dialog-submit">
                    {submitting ? <LoaderCircle className="code-mode-spin" size={15} /> : null}
                    {submitting ? (operation === 'push' ? '推送中' : '处理中') : '确定'}
                  </button>
                </footer>
              </form>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
