export type ProjectDirectoryPickResult =
  | { ok: true; path: string; name: string }
  | { ok: false; reason: 'unsupported' | 'cancelled' | 'failed'; message?: string };

function getProjectDirectoryApi() {
  return window.pywebview?.api?.select_project_directory;
}

export function isProjectDirectoryPickerSupported(): boolean {
  return typeof getProjectDirectoryApi() === 'function';
}

export function getDirectoryName(path: string): string {
  const normalized = path.trim().replace(/[\\/]+$/, '');
  const parts = normalized.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] || normalized;
}

export function isLikelyAbsolutePath(path: string): boolean {
  const trimmed = path.trim();
  return (
    trimmed.startsWith('/') ||
    /^[A-Za-z]:[\\/]/.test(trimmed) ||
    /^\\\\[^\\]+\\[^\\]+/.test(trimmed)
  );
}

export async function selectProjectDirectory(): Promise<ProjectDirectoryPickResult> {
  const pickDirectory = getProjectDirectoryApi();
  if (!pickDirectory) {
    return { ok: false, reason: 'unsupported' };
  }

  try {
    const selectedPath = await pickDirectory();
    if (!selectedPath) {
      return { ok: false, reason: 'cancelled' };
    }
    const path = selectedPath.trim();
    if (!path) {
      return { ok: false, reason: 'cancelled' };
    }
    return { ok: true, path, name: getDirectoryName(path) };
  } catch (error) {
    return {
      ok: false,
      reason: 'failed',
      message: error instanceof Error ? error.message : String(error),
    };
  }
}
