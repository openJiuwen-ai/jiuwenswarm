// 目录过滤名单：按需手动追加。
export const IGNORED_DIRECTORY_NAMES = [
  '__pycache__',
  '.vscode',
  '.cursor',
  '.vscod',
  '.curosr',
];

const IGNORED_DIRECTORY_SET = new Set<string>(IGNORED_DIRECTORY_NAMES);

export const containsIgnoredDirectory = (targetPath: string) =>
  targetPath
    .split('/')
    .filter(Boolean)
    .some((segment) => IGNORED_DIRECTORY_SET.has(segment));
