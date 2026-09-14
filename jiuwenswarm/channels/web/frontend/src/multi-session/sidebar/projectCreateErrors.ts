export function projectCreateErrorKey(error: unknown): string | null {
  const message = error instanceof Error ? error.message : String(error);
  const code = error && typeof error === 'object' && 'code' in error
    ? String((error as { code?: unknown }).code || '')
    : '';
  if (message.includes('project_dir already exists')) {
    return 'multiSession.project.errors.pathExists';
  }
  if (message.includes('project name already exists')) {
    return 'multiSession.project.errors.nameExists';
  }
  if (code === 'PROJECT_DIR_MISSING' || message.includes('project directory does not exist')) {
    return 'multiSession.project.errors.pathMissing';
  }
  // 命中已归档项目但无法自动恢复（如后端未返回 project_id）时的兜底提示；
  // 正常路径由 workspaceStore.createProject 自动调用 project.unarchive 恢复。
  if (code === 'PROJECT_ARCHIVED') {
    return 'multiSession.project.errors.projectArchived';
  }
  return null;
}
