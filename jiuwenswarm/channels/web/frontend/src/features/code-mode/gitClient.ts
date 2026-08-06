import { webRequest } from '../../services/webClient';
import type { GitCommitResult, GitPushResult, GitRepoStatus, GitTurnDiff, GitTurnDiffList, ProjectGitDiffStatus } from './types';

export const gitClient = {
  status: (projectId: string) =>
    webRequest<GitRepoStatus>('project.git.status', {
      project_id: projectId,
    }),

  probe: (projectId: string) =>
    webRequest<GitRepoStatus>('project.git.probe', {
      project_id: projectId,
    }),

  init: (projectId: string, initialBranch = 'main') =>
    webRequest<GitRepoStatus>('project.git.init', {
      project_id: projectId,
      initial_branch: initialBranch,
    }),

  switchBranch: (projectId: string, branch: string) =>
    webRequest<{
      switched: boolean;
      previous_branch: string | null;
      current_branch: string;
      status: GitRepoStatus;
    }>('project.git.switch_branch', {
      project_id: projectId,
      branch,
      require_clean: true,
    }),

  createBranch: (projectId: string, branch: string, startPoint?: string | null) =>
    webRequest<{
      created: boolean;
      checked_out: boolean;
      branch: string;
      status: GitRepoStatus;
    }>('project.git.create_branch', {
      project_id: projectId,
      branch,
      checkout: true,
      ...(startPoint ? { start_point: startPoint } : {}),
    }),

  commit: (projectId: string, message: string, options: { stageAll?: boolean; paths?: string[]; amend?: boolean; noVerify?: boolean } = {}) =>
    webRequest<GitCommitResult>(
      'project.git.commit',
      {
        project_id: projectId,
        message,
        stage_all: options.stageAll ?? !options.paths,
        amend: options.amend ?? false,
        no_verify: options.noVerify ?? false,
        ...(options.paths ? { paths: options.paths } : {}),
      },
      { timeoutMs: 60_000 },
    ),

  push: (projectId: string, options: { remote?: string; branch?: string; setUpstream?: boolean; force?: boolean; delete?: boolean } = {}) =>
    webRequest<GitPushResult>(
      'project.git.push',
      {
        project_id: projectId,
        remote: options.remote ?? 'origin',
        ...(options.branch ? { branch: options.branch } : {}),
        set_upstream: options.setUpstream ?? false,
        force: options.force ?? false,
        delete: options.delete ?? false,
      },
      { timeoutMs: 90_000 },
    ),

  diffStatus: (projectId: string, sessionId: string, options: { includeFiles?: boolean; includeHunks?: boolean } = {}) =>
    webRequest<ProjectGitDiffStatus>('project.git.diff_status', {
      project_id: projectId,
      session_id: sessionId,
      include_files: options.includeFiles ?? false,
      include_hunks: options.includeHunks ?? false,
    }),

  turnDiffList: (projectId: string, sessionId: string, options: { limit?: number; cursor?: number } = {}) =>
    webRequest<GitTurnDiffList>('project.git.turn_diff_list', {
      project_id: projectId,
      session_id: sessionId,
      limit: options.limit ?? 50,
      cursor: options.cursor ?? 0,
    }),

  turnDiff: (
    projectId: string,
    sessionId: string,
    target: { changeSetId?: string; turnIndex?: number },
    options: { includeFiles?: boolean; includeHunks?: boolean } = {}
  ) =>
    webRequest<GitTurnDiff>('project.git.turn_diff', {
      project_id: projectId,
      session_id: sessionId,
      ...(target.changeSetId ? { change_set_id: target.changeSetId } : { turn_index: target.turnIndex }),
      include_files: options.includeFiles ?? true,
      include_hunks: options.includeHunks ?? true,
    }),
};
