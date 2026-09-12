export type ProjectDirectoryPickResult =
  | { ok: true; path: string; name: string }
  | { ok: false; reason: 'unsupported' | 'cancelled' | 'failed'; message?: string };

export type ProjectFilePickResult =
  | { ok: true; path: string; name: string }
  | { ok: false; reason: 'unsupported' | 'cancelled' | 'failed'; message?: string };

export interface SelectProjectFileOptions {
  initialPath?: string;
  title?: string;
}

const DIRECTORY_PICKER_TIMEOUT_MS = 10 * 60 * 1000;
const FILE_PICKER_TIMEOUT_MS = 10 * 60 * 1000;

function getProjectDirectoryApi() {
  if (typeof window === 'undefined') return undefined;
  return window.pywebview?.api?.select_project_directory;
}

function getProjectFileApi() {
  if (typeof window === 'undefined') return undefined;
  return window.pywebview?.api?.select_local_file_path;
}

export function isProjectDirectoryPickerSupported(): boolean {
  // 桌面端走 pywebview；浏览器 / whl 走后端 path.select_directory
  return typeof getProjectDirectoryApi() === 'function' || typeof window !== 'undefined';
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

function normalizePickedPath(selectedPath: string | null | undefined): ProjectDirectoryPickResult {
  if (!selectedPath) {
    return { ok: false, reason: 'cancelled' };
  }
  const path = selectedPath.trim();
  if (!path) {
    return { ok: false, reason: 'cancelled' };
  }
  return { ok: true, path, name: getDirectoryName(path) };
}

function normalizePickedFilePath(selectedPath: string | null | undefined): ProjectFilePickResult {
  if (!selectedPath) {
    return { ok: false, reason: 'cancelled' };
  }
  const path = selectedPath.trim();
  if (!path) {
    return { ok: false, reason: 'cancelled' };
  }
  return { ok: true, path, name: getDirectoryName(path) };
}

async function selectProjectDirectoryViaBackend(
  initialDir?: string,
): Promise<ProjectDirectoryPickResult> {
  try {
    const { webRequest } = await import('../../services/webClient');
    const payload = await webRequest<{ path?: string | null; cancelled?: boolean }>(
      'path.select_directory',
      initialDir ? { initial_dir: initialDir } : {},
      { timeoutMs: DIRECTORY_PICKER_TIMEOUT_MS },
    );
    if (payload?.cancelled || !payload?.path) {
      return { ok: false, reason: 'cancelled' };
    }
    return normalizePickedPath(payload.path);
  } catch (error) {
    const code =
      error && typeof error === 'object' && 'code' in error
        ? String((error as { code?: unknown }).code || '')
        : '';
    if (code === 'UNSUPPORTED') {
      return {
        ok: false,
        reason: 'unsupported',
        message: error instanceof Error ? error.message : String(error),
      };
    }
    return {
      ok: false,
      reason: 'failed',
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export async function selectProjectDirectory(
  options?: { initialDir?: string },
): Promise<ProjectDirectoryPickResult> {
  const pickDirectory = getProjectDirectoryApi();
  if (typeof pickDirectory === 'function') {
    try {
      const selectedPath = await pickDirectory();
      return normalizePickedPath(selectedPath);
    } catch (error) {
      return {
        ok: false,
        reason: 'failed',
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  return selectProjectDirectoryViaBackend(options?.initialDir);
}

async function selectProjectFileViaBackend(
  options: SelectProjectFileOptions,
): Promise<ProjectFilePickResult> {
  try {
    const { webRequest } = await import('../../services/webClient');
    const payload = await webRequest<{ path?: string | null; cancelled?: boolean }>(
      'path.select_file',
      {
        ...(options.initialPath ? { initial_path: options.initialPath } : {}),
        ...(options.title ? { title: options.title } : {}),
      },
      { timeoutMs: FILE_PICKER_TIMEOUT_MS },
    );
    if (payload?.cancelled || !payload?.path) {
      return { ok: false, reason: 'cancelled' };
    }
    return normalizePickedFilePath(payload.path);
  } catch (error) {
    const code =
      error && typeof error === 'object' && 'code' in error
        ? String((error as { code?: unknown }).code || '')
        : '';
    return {
      ok: false,
      reason: code === 'UNSUPPORTED' || code === 'METHOD_NOT_FOUND' ? 'unsupported' : 'failed',
      message: error instanceof Error ? error.message : String(error),
    };
  }
}

export async function selectProjectFile(
  options: SelectProjectFileOptions = {},
): Promise<ProjectFilePickResult> {
  const pickFile = getProjectFileApi();
  if (typeof pickFile === 'function') {
    try {
      const selectedPath = await pickFile(options.initialPath ?? null, options.title ?? null);
      return normalizePickedFilePath(selectedPath);
    } catch (error) {
      return {
        ok: false,
        reason: 'failed',
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  return selectProjectFileViaBackend(options);
}
