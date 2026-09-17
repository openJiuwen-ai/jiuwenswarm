import type { WebError } from '../../types';

export type GitPublishOperation = 'commit' | 'commit_push' | 'push';

export function defaultCommitMessage(filesChanged: number): string {
  if (filesChanged <= 0) return 'Update project files';
  return filesChanged === 1 ? 'Update 1 file' : `Update ${filesChanged} files`;
}

export function remoteNames(remoteBranches: string[]): string[] {
  const names = remoteBranches.map(branch => branch.split('/', 1)[0]?.trim()).filter((name): name is string => Boolean(name));
  return [...new Set(names.length > 0 ? names : ['origin'])];
}

const ERROR_MESSAGES: Record<string, string> = {
  NOTHING_TO_COMMIT: '没有可提交的修改。若修改尚未暂存，请勾选“包含未暂存的更改”。',
  GIT_TRANSIENT_STATE: '仓库正在合并、变基或执行其他 Git 操作，请完成后重试。',
  DETACHED_HEAD: '当前处于 detached HEAD，请选择一个本地分支后再推送。',
  BRANCH_ALREADY_EXISTS: '分支名称已存在，请换一个名称。',
  BRANCH_INVALID: '分支名称不符合 Git 规范，请检查后重试。',
  REMOTE_NOT_FOUND: '远程仓库不存在，请检查远程名称或 Git 配置。',
  PUSH_REJECTED: '远程仓库拒绝了推送，请先同步远程修改并检查分支保护或权限。',
  GIT_COMMAND_TIMEOUT: 'Git 操作超时，请检查仓库状态和网络后重试。',
  NOT_GIT_REPOSITORY: '当前项目不是 Git 仓库。',
  GIT_NOT_FOUND: '当前环境未安装 Git。',
  PROJECT_DIR_MISSING: '项目目录不存在。',
};

export function gitPublishErrorMessage(error: unknown, fallback: string): string {
  const webError = error as WebError | null;
  if (webError?.code && ERROR_MESSAGES[webError.code]) return ERROR_MESSAGES[webError.code];
  if (webError instanceof Error && webError.message.trim()) return webError.message;
  return fallback;
}
