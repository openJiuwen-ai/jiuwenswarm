import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { AlertCircle, Check, LoaderCircle, Plus, Search, X } from 'lucide-react';
import CodehubBranchIcon from '../../assets/code-mode/codehub-branch.svg?react';
import type { ProjectInfo, WebError } from '../../types';
import { useWorkspaceStore } from '../../stores';
import { gitClient } from './gitClient';
import type { GitDiffRepoInfo, GitRepoStatus } from './types';
import './CodeMode.css';

interface CodeBranchSelectorProps {
  project: ProjectInfo | null;
  compact?: boolean;
  disabled?: boolean;
  variant?: 'default' | 'environment';
  liveRepo?: GitDiffRepoInfo | null;
}

function getErrorMessage(error: unknown): string {
  const webError = error as WebError;
  switch (webError.code) {
    case 'WORKTREE_DIRTY':
      return '工作区存在未提交修改，请处理后再切换分支。';
    case 'GIT_TRANSIENT_STATE':
      return '仓库正在合并或变基，暂时不能切换分支。';
    case 'BRANCH_NOT_FOUND':
      return '分支不存在，请刷新后重试。';
    case 'BRANCH_ALREADY_EXISTS':
      return '分支名称已存在。';
    case 'GIT_NOT_FOUND':
      return '当前环境未安装 Git，无法使用代码模式分支能力。';
    default:
      return webError.message || 'Git 操作失败，请稍后重试。';
  }
}

export function CodeBranchSelector({ project, compact = false, disabled = false, variant = 'default', liveRepo = null }: CodeBranchSelectorProps) {
  const [status, setStatus] = useState<GitRepoStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [operating, setOperating] = useState(false);
  const [notGit, setNotGit] = useState(false);
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [branchDraft, setBranchDraft] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);
  const unbornHintId = useId();
  const loadProjects = useWorkspaceStore(state => state.loadProjects);

  const closeMenu = useCallback(() => {
    setOpen(false);
    setError(null);
  }, []);

  const loadStatus = useCallback(async () => {
    if (!project || project.work_mode !== 'code' || project.is_default) {
      setStatus(null);
      return;
    }
    setLoading(true);
    setError(null);
    setNotGit(false);
    try {
      const nextStatus =
        project.git.status === 'ready' || project.git.enabled ? await gitClient.status(project.project_id) : await gitClient.probe(project.project_id);
      setStatus(nextStatus);
      setNotGit(!nextStatus.repo.is_git);
    } catch (nextError) {
      setStatus(null);
      if ((nextError as WebError).code === 'NOT_GIT_REPOSITORY') {
        setNotGit(true);
      } else {
        setError(getErrorMessage(nextError));
      }
    } finally {
      setLoading(false);
    }
  }, [project]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    if (!liveRepo || !project || project.work_mode !== 'code' || project.is_default) return;
    setNotGit(!liveRepo.is_git);
    setStatus(previous => {
      if (!previous) return previous;
      return {
        ...previous,
        repo: {
          ...previous.repo,
          is_git: liveRepo.is_git,
          repo_root: liveRepo.repo_root,
          branch: liveRepo.branch,
          head: liveRepo.head,
          transient: liveRepo.transient,
        },
        branches: {
          ...previous.branches,
          current: liveRepo.branch,
        },
      };
    });
  }, [liveRepo, project]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeMenu();
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [closeMenu, open]);

  const branches = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return (status?.branches.locals ?? []).filter(branch => !query || branch.toLocaleLowerCase().includes(query));
  }, [search, status]);

  const isNotGit = notGit || status?.repo.is_git === false || Boolean(!loading && !status && project?.git.status === 'not_git');
  const currentBranch = liveRepo?.branch || status?.branches.current || project?.git.branch || '';
  const branchWritesBlocked = Boolean(status?.repo.transient || status?.repo.detached);
  const isUnbornHead = Boolean(status?.repo.is_git && currentBranch && !status.repo.head);

  const openMenu = () => {
    setOpen(true);
    void loadStatus();
  };

  const initializeGit = async () => {
    if (!project) return;
    setOperating(true);
    setError(null);
    try {
      const nextStatus = await gitClient.init(project.project_id, 'main');
      setStatus(nextStatus);
      setNotGit(false);
      await loadProjects();
      setOpen(true);
    } catch (nextError) {
      setError(getErrorMessage(nextError));
    } finally {
      setOperating(false);
    }
  };

  const switchBranch = async (branch: string) => {
    if (!project || branch === currentBranch) {
      closeMenu();
      return;
    }
    setOperating(true);
    setError(null);
    try {
      const result = await gitClient.switchBranch(project.project_id, branch);
      setStatus(result.status);
      closeMenu();
      await loadProjects();
    } catch (nextError) {
      setError(getErrorMessage(nextError));
    } finally {
      setOperating(false);
    }
  };

  const createBranch = async () => {
    if (!project || !branchDraft.trim()) return;
    setOperating(true);
    setError(null);
    try {
      const result = await gitClient.createBranch(project.project_id, branchDraft.trim(), currentBranch || undefined);
      setStatus(result.status);
      setBranchDraft('');
      setCreateOpen(false);
      closeMenu();
      await loadProjects();
    } catch (nextError) {
      setError(getErrorMessage(nextError));
    } finally {
      setOperating(false);
    }
  };

  if (!project || project.work_mode !== 'code' || project.is_default) return null;

  return (
    <div ref={rootRef} className={`code-branch${compact ? ' code-branch--compact' : ''}${variant === 'environment' ? ' code-branch--environment' : ''}`} data-testid="code-mode-branch-selector">
      {isNotGit ? (
        <button
          type='button'
          className='code-branch__trigger code-branch__trigger--warning'
          onClick={() => void initializeGit()}
          disabled={disabled || operating}
          title='该目录还不是 Git 仓库'
          data-testid='code-mode-branch-trigger'
          data-variant='init'
        >
          {operating ? <LoaderCircle className='code-mode-spin' size={15} /> : <CodehubBranchIcon className='h-[15px] w-[15px]' />}
          <span>初始化 Git</span>
        </button>
      ) : (
        <button
          type='button'
          className='code-branch__trigger'
          onClick={() => (open ? closeMenu() : openMenu())}
          disabled={disabled || loading || operating || !status}
          aria-haspopup='menu'
          aria-expanded={open}
          title={currentBranch || '加载分支'}
          data-testid='code-mode-branch-trigger'
          data-variant='switch'
        >
          {loading || operating ? <LoaderCircle className='code-mode-spin' size={15} /> : <CodehubBranchIcon className='h-[15px] w-[15px]' />}
          <span>{currentBranch || '加载分支'}</span>
          <svg className={open ? 'code-branch__chevron is-open' : 'code-branch__chevron'} width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
          </svg>
        </button>
      )}

      {open && status ? (
        <div className='code-branch__menu' role='menu' data-testid='code-mode-branch-menu'>
          <label className='code-branch__search' data-testid='code-mode-branch-search'>
            <Search size={15} />
            <input value={search} onChange={event => setSearch(event.target.value)} placeholder='搜索分支' autoFocus data-testid='code-mode-branch-search-input' />
          </label>
          <div className='code-branch__section-label' data-testid='code-mode-branch-section-label'>分支</div>
          <div className='code-branch__list' data-testid='code-mode-branch-list'>
            {branches.map(branch => (
              <button
                type='button'
                key={branch}
                className={branch === currentBranch ? 'code-branch__option is-active' : 'code-branch__option'}
                onClick={() => void switchBranch(branch)}
                disabled={branchWritesBlocked}
                role='menuitemradio'
                aria-checked={branch === currentBranch}
                data-testid='code-mode-branch-option'
                data-variant={branch}
              >
                <CodehubBranchIcon className='h-[15px] w-[15px]' />
                <span>{branch}</span>
                {branch === currentBranch ? <Check size={16} /> : null}
              </button>
            ))}
            {branches.length === 0 ? <div className='code-branch__empty' data-testid='code-mode-branch-list-empty'>没有匹配的本地分支</div> : null}
          </div>
          {branchWritesBlocked ? (
            <div className='code-branch__menu-error' role='status' data-testid='code-mode-branch-menu-warning'>
              <AlertCircle size={14} />
              <span>{status.repo.transient ? '仓库正在合并或变基，暂时不能切换分支。' : '当前处于 detached HEAD，暂时不能切换分支。'}</span>
            </div>
          ) : null}
          {error ? (
            <div className='code-branch__menu-error' role='alert' data-testid='code-mode-branch-menu-error'>
              <AlertCircle size={14} />
              <span>{error}</span>
              <button type='button' onClick={() => setError(null)} aria-label='关闭提示' data-testid='code-mode-branch-menu-error-dismiss'>
                <X size={13} />
              </button>
            </div>
          ) : null}
          <div
            className={isUnbornHead ? 'code-branch__create-wrap is-disabled' : 'code-branch__create-wrap'}
            tabIndex={isUnbornHead ? 0 : undefined}
            aria-describedby={isUnbornHead ? unbornHintId : undefined}
            data-testid='code-mode-branch-create-wrap'
          >
            <button type='button' className='code-branch__create' onClick={() => setCreateOpen(true)} disabled={branchWritesBlocked || isUnbornHead} data-testid='code-mode-branch-create-button'>
              <Plus size={16} />
              <span>创建并检出新分支</span>
            </button>
            {isUnbornHead ? (
              <span id={unbornHintId} className='code-branch__create-hint' role='tooltip' data-testid='code-mode-branch-create-hint'>
                空仓库需要完成首次提交后才能创建其他分支
              </span>
            ) : null}
          </div>
        </div>
      ) : null}

      {!open && branchWritesBlocked ? (
        <div className='code-branch__error' role='status' data-testid='code-mode-branch-status-warning'>
          <AlertCircle size={14} />
          <span>{status?.repo.transient ? '仓库正在合并或变基，暂时不能切换分支。' : '当前处于 detached HEAD，暂时不能切换分支。'}</span>
        </div>
      ) : null}

      {!open && error ? (
        <div className='code-branch__error' role='alert' data-testid='code-mode-branch-status-error'>
          <AlertCircle size={14} />
          <span>{error}</span>
          <button type='button' onClick={() => setError(null)} aria-label='关闭提示' data-testid='code-mode-branch-status-error-dismiss'>
            <X size={13} />
          </button>
        </div>
      ) : null}

      {createOpen ? (
        <div className='code-mode-dialog-backdrop' role='presentation' data-testid='code-mode-create-branch-dialog-backdrop'>
          <form
            className='code-mode-dialog'
            onSubmit={event => {
              event.preventDefault();
              void createBranch();
            }}
            data-testid='code-mode-create-branch-dialog'
          >
            <div className='code-mode-dialog__header' data-testid='code-mode-create-branch-dialog-header'>
              <h3 data-testid='code-mode-create-branch-dialog-title'>创建并检出分支</h3>
              <button type='button' onClick={() => setCreateOpen(false)} aria-label='关闭' data-testid='code-mode-create-branch-dialog-close'>
                <X size={18} />
              </button>
            </div>
            <input value={branchDraft} onChange={event => setBranchDraft(event.target.value)} placeholder='请输入分支名称，如：jiuwen/xxx，分支名不能以“/”结尾' autoFocus data-testid='code-mode-create-branch-dialog-input' />
            {error ? <div className='code-mode-dialog__error' data-testid='code-mode-create-branch-dialog-error'>{error}</div> : null}
            <div className='code-mode-dialog__actions' data-testid='code-mode-create-branch-dialog-actions'>
              <button type='button' className='code-mode-button' onClick={() => setCreateOpen(false)} data-testid='code-mode-create-branch-dialog-cancel'>
                取消
              </button>
              <button type='submit' className='code-mode-button code-mode-button--primary' disabled={!branchDraft.trim() || operating} data-testid='code-mode-create-branch-dialog-submit'>
                {operating ? '创建中…' : '确定'}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}
