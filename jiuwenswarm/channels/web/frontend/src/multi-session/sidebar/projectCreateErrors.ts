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
  return null;
}
