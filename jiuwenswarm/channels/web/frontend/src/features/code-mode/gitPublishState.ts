import type { TFunction } from 'i18next';
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

/** 错误码 → i18n 键。未映射的错误码回退到原始 message，再回退到调用方 fallback。 */
const ERROR_KEYS: Record<string, string> = {
  NOTHING_TO_COMMIT: 'codeMode.publishErrors.nothingToCommit',
  GIT_TRANSIENT_STATE: 'codeMode.publishErrors.transientState',
  DETACHED_HEAD: 'codeMode.publishErrors.detachedHead',
  BRANCH_ALREADY_EXISTS: 'codeMode.publishErrors.branchAlreadyExists',
  BRANCH_INVALID: 'codeMode.publishErrors.branchInvalid',
  REMOTE_NOT_FOUND: 'codeMode.publishErrors.remoteNotFound',
  PUSH_REJECTED: 'codeMode.publishErrors.pushRejected',
  GIT_COMMAND_TIMEOUT: 'codeMode.publishErrors.gitTimeout',
  NOT_GIT_REPOSITORY: 'codeMode.publishErrors.notGitRepository',
  GIT_NOT_FOUND: 'codeMode.publishErrors.gitNotFound',
  PROJECT_DIR_MISSING: 'codeMode.publishErrors.projectDirMissing',
};

export function gitPublishErrorMessage(error: unknown, fallback: string, t: TFunction): string {
  const webError = error as WebError | null;
  const key = webError?.code ? ERROR_KEYS[webError.code] : undefined;
  if (key) return t(key);
  if (webError instanceof Error && webError.message.trim()) return webError.message;
  return fallback;
}
