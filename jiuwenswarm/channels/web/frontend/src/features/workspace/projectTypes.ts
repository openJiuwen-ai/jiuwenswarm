export type WorkMode = 'work' | 'code';

export type ProjectGitSnapshot = {
  enabled: boolean;
  repo_root: string;
  initialized_by_jiuwenswarm: boolean;
  detected_at: number;
  status: 'disabled' | 'unknown' | 'ready' | 'not_git' | 'error' | string;
  branch: string;
  error: string;
  is_dirty: boolean;
};

export type ProjectInfo = {
  project_id: string;
  name: string;
  project_dir: string;
  pinned: boolean;
  pin_order: number;
  is_default: boolean;
  hidden: boolean;
  work_mode: WorkMode;
  git: ProjectGitSnapshot;
  session_count: number;
  last_message_at: number | null;
  last_user_message_at: number | null;
  created_at: number;
  updated_at?: number;
};
