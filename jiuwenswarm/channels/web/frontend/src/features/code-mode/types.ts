export interface GitRepoStatus {
  project_id: string;
  project_name: string;
  project_dir: string;
  work_mode: 'code';
  repo: {
    is_git: boolean;
    repo_root: string | null;
    branch: string | null;
    head: string | null;
    detached: boolean;
    transient: boolean;
    upstream: string | null;
  };
  working_tree: {
    is_dirty: boolean;
    staged: number;
    unstaged: number;
    untracked: number;
    conflicted: number;
  };
  branches: {
    current: string | null;
    locals: string[];
    remotes: string[];
  };
  generated_at: number;
}

export interface GitDiffHunk {
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  lines: string[];
}

export interface GitDiffFile {
  file_path: string;
  status: 'modified' | 'added' | 'deleted' | 'renamed' | 'missing' | string;
  change_type?: 'modified' | 'added' | 'deleted' | 'renamed' | 'missing' | string;
  lines_added: number;
  lines_removed: number;
  is_binary: boolean;
  is_new_file: boolean;
  is_deleted_file: boolean;
  is_untracked: boolean;
  is_large_file: boolean;
  is_truncated: boolean;
  hunks: GitDiffHunk[];
}

export interface GitDiffSummary {
  kind: 'working_tree';
  is_dirty: boolean;
  stats: GitDiffStats;
  files: Record<string, GitDiffFile>;
}

export interface GitDiffStats {
  files_changed: number;
  lines_added: number;
  lines_removed: number;
}

export interface GitTurnDiff {
  kind: 'conversation_turn';
  change_set_id: string;
  turn_index: number;
  request_id: string;
  user_message_id: string;
  assistant_message_id: string;
  timestamp: string;
  user_prompt_preview: string;
  repo_root?: string | null;
  branch?: string | null;
  base_head?: string | null;
  status: 'completed' | 'discarded' | 'partial' | 'failed' | string;
  stats: GitDiffStats;
  files: Record<string, GitDiffFile>;
}

export interface GitTurnDiffList {
  project_id: string;
  session_id: string;
  repo_root: string | null;
  branch: string | null;
  base_head: string | null;
  turns: GitTurnDiff[];
  cursor: number;
  next_cursor: number;
  has_more: boolean;
  limit: number;
  total: number;
}

export type GitTurnChangeAction = 'discard' | 'redo';

export interface GitDiscardTurnChangesResult {
  session_id: string;
  turn_index: number;
  change_set_id: string | null;
  restored_files: string[];
  deleted_files: string[];
  errors: unknown[];
  file_ops_truncated: boolean;
  global_file_ops_truncated: false;
  partial: boolean;
}

export interface GitRedoTurnChangesResult {
  session_id: string;
  turn_index: number;
  change_set_id: string | null;
  redone_files: string[];
  deleted_files: string[];
  errors: unknown[];
  partial: boolean;
}

export interface GitCommitResult {
  committed: true;
  commit_hash: string | null;
  amended: boolean;
  status: GitRepoStatus;
}

export interface GitPushResult {
  pushed: true;
  remote: string;
  branch: string | null;
  deleted: boolean;
  upstream_set: boolean;
  status: GitRepoStatus;
}

export type CodeReviewTarget =
  | {
      source: 'last_turn';
      changeSetId: string;
      turnIndex: number;
    }
  | {
      source: 'working_tree';
    };

export interface GitDiffRepoInfo {
  is_git: boolean;
  repo_root: string | null;
  branch: string | null;
  head: string | null;
  transient: boolean;
}

export interface GitDiffWatchSnapshot {
  project_id: string;
  session_id: string | null;
  repo: GitDiffRepoInfo;
  current: GitDiffSummary | null;
  last_turn: GitTurnDiff | null;
  revision: string;
}

export interface GitDiffWatchResponse {
  watch_id: string;
  scope: 'summary';
  snapshot: GitDiffWatchSnapshot;
}

export interface GitDiffFilesWatchResponse {
  watch_id: string;
  files_scope: {
    source: 'current' | 'last_turn';
  };
  revision: string;
  files: Record<string, GitDiffFile>;
}

export interface GitDiffDetailWatchResponse {
  watch_id: string;
  detail_scope: {
    source: 'current' | 'last_turn';
    files: string[];
  };
  revision: string;
  files: Record<string, GitDiffFile | null>;
}

export interface ProjectGitDiffStatus {
  project_id: string;
  session_id: string | null;
  work_mode: 'code';
  repo: GitDiffRepoInfo;
  current: GitDiffSummary | null;
  last_turn: GitTurnDiff | null;
  generated_at: number;
}
