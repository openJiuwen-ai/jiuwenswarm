import { useEffect, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
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
  const { t } = useTranslation();
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
        if (!disposed) setError(gitPublishErrorMessage(nextError, t('codeMode.publish.loadStatusFailed'), t));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [open, project.project_id, t]);

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
    ? t('codeMode.publish.busyProcessing')
    : !isGit
      ? t('codeMode.publish.notGitRepoTitle')
      : transient
        ? t('codeMode.publish.repoBusy')
        : t('codeMode.publish.title');
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
      setError(gitPublishErrorMessage(nextError, t('codeMode.publish.createBranchFailed'), t));
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
      const hashSuffix = committedHash ? ` (${committedHash})` : '';
      setNotice(operation === 'commit' ? `${t('codeMode.publish.commitSucceeded')}${hashSuffix}` : operation === 'push' ? t('codeMode.publish.pushSucceeded') : `${t('codeMode.publish.commitPushSucceeded')}${hashSuffix}`);
    } catch (nextError) {
      const detail = gitPublishErrorMessage(nextError, phase === 'commit' ? t('codeMode.publish.commitFailed') : t('codeMode.publish.pushFailed'), t);
      if (committedHash && phase === 'push') {
        void Promise.resolve(onSuccess()).catch(() => undefined);
        setOperation('push');
        setError(`${t('codeMode.publish.commitOkPushFailed', { hash: committedHash })}${detail}`);
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
          <span>{t('codeMode.publish.title')}</span>
        </button>
      ) : (
        <button type="button" className="code-review__publish-button" onClick={() => setOpen(true)} disabled={triggerDisabled} title={disabledReason} data-testid="code-mode-publish-trigger" data-variant="review">
          {t('codeMode.publish.title')}
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
                  <h3 id="code-publish-title" data-testid="code-mode-publish-dialog-title">{t('codeMode.publish.title')}</h3>
                  <button type="button" onClick={() => setOpen(false)} disabled={submitting || creatingBranch} aria-label={t('codeMode.common.close')} data-testid="code-mode-publish-dialog-close">
                    <X size={18} />
                  </button>
                </header>

                {loading ? (
                  <div className="code-publish-dialog__loading" data-testid="code-mode-publish-dialog-loading">
                    <LoaderCircle className="code-mode-spin" size={18} />
                    <span>{t('codeMode.publish.loadingGitStatus')}</span>
                  </div>
                ) : (
                  <div className="code-publish-dialog__fields" data-testid="code-mode-publish-dialog-fields">
                    <div className="code-publish-field" data-testid="code-mode-publish-field-branch">
                      <span>{t('codeMode.publish.targetBranch')}</span>
                      <div className="code-publish-branch-picker">
                        <select
                          value={selectedBranch}
                          onChange={event => {
                            setSelectedBranch(event.target.value);
                            if (event.target.value !== currentBranch) setSetUpstream(true);
                          }}
                          disabled={submitting || creatingBranch || includesCommit}
                          aria-label={t('codeMode.publish.targetBranch')}
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
                          title={isUnbornHead ? t('codeMode.branchSelector.unbornHint') : t('codeMode.branchSelector.createAndCheckout')}
                          data-testid="code-mode-publish-branch-create-trigger"
                        >
                          <Plus size={15} />
                          <span>{t('codeMode.publish.newBranch')}</span>
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
                            placeholder={t('codeMode.publish.branchNameExample')}
                            maxLength={255}
                            disabled={creatingBranch || submitting}
                            aria-label={t('codeMode.publish.newBranchName')}
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
                            {t('codeMode.common.cancel')}
                          </button>
                          <button
                            type="button"
                            className="code-mode-button code-mode-button--primary"
                            onClick={() => void createBranch()}
                            disabled={!branchDraft.trim() || creatingBranch || submitting}
                            data-testid="code-mode-publish-branch-create-submit"
                          >
                            {creatingBranch ? <LoaderCircle className="code-mode-spin" size={14} /> : null}
                            {creatingBranch ? t('codeMode.publish.creatingShort') : t('codeMode.publish.createShort')}
                          </button>
                        </div>
                      ) : null}
                    </div>

                    {includesPush ? (
                      <label className="code-publish-field" data-testid="code-mode-publish-field-remote">
                        <span>{t('codeMode.publish.remote')}</span>
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
                        <span>{t('codeMode.publish.commitMessage')}</span>
                        <textarea
                          value={message}
                          onChange={event => setMessage(event.target.value)}
                          placeholder={t('codeMode.publish.messagePlaceholder', { example: defaultCommitMessage(filesChanged) })}
                          maxLength={200}
                          disabled={submitting}
                          data-testid="code-mode-publish-field-message-input"
                        />
                        <small data-testid="code-mode-publish-field-message-count">{message.length}/200</small>
                      </label>
                    ) : null}

                    <fieldset className="code-publish-operations" data-testid="code-mode-publish-operations">
                      <legend data-testid="code-mode-publish-operations-legend">{t('codeMode.publish.operationType')}</legend>
                      {(
                        [
                          ['commit', t('codeMode.publish.operationCommit')],
                          ['commit_push', t('codeMode.publish.operationCommitPush')],
                          ['push', t('codeMode.publish.operationPush')],
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
                          <span>{t('codeMode.publish.includeUnstaged')}</span>
                        </label>
                      ) : null}
                      {includesPush ? (
                        <label data-testid="code-mode-publish-option-set-upstream">
                          <input type="checkbox" checked={setUpstream} onChange={event => setSetUpstream(event.target.checked)} disabled={submitting} />
                          <span>{t('codeMode.publish.setUpstream')}</span>
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
                    {t('codeMode.common.cancel')}
                  </button>
                  <button type="submit" className="code-mode-button code-mode-button--primary" disabled={!canSubmit} data-testid="code-mode-publish-dialog-submit">
                    {submitting ? <LoaderCircle className="code-mode-spin" size={15} /> : null}
                    {submitting ? (operation === 'push' ? t('codeMode.publish.pushing') : t('codeMode.publish.processing')) : t('codeMode.common.confirm')}
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
