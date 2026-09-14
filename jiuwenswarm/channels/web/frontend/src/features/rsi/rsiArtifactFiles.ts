import type { RsiArtifactFileGetResult, RsiTreeNode } from './types';
import { rsiArtifactFilesGet, rsiArtifactFilesList } from './rsiApi';

export interface RsiArtifactSource {
  taskId: string;
  path: string;
  initialFilePath: string | null;
}

export interface RsiArtifactFileEntry {
  name: string;
  path: string;
  relativePath?: string;
  isDirectory: boolean;
  size: number;
  mimeType?: string;
}

export interface RsiArtifactLoadResult {
  entries: RsiArtifactFileEntry[];
  initialFilePath: string | null;
}

const BINARY_EXTENSIONS = new Set([
  '7z', 'avi', 'bin', 'bz2', 'class', 'dat', 'db', 'dll', 'dylib', 'eot', 'exe', 'gz',
  'jar', 'mov', 'mp3', 'mp4', 'otf', 'pyc', 'rar', 'so', 'sqlite', 'tar', 'ttf', 'wasm',
  'war', 'woff', 'woff2', 'xz', 'zip',
]);

const MIME_BY_EXTENSION: Record<string, string> = {
  bib: 'text/plain',
  csv: 'text/csv',
  css: 'text/css',
  diff: 'text/plain',
  htm: 'text/html',
  html: 'text/html',
  js: 'text/javascript',
  json: 'application/json',
  jsonl: 'application/x-ndjson',
  jsx: 'text/javascript',
  md: 'text/markdown',
  markdown: 'text/markdown',
  log: 'text/plain',
  pdf: 'application/pdf',
  png: 'image/png',
  py: 'text/x-python',
  sh: 'text/plain',
  sty: 'text/plain',
  tex: 'text/plain',
  toml: 'text/plain',
  ts: 'text/typescript',
  tsx: 'text/typescript',
  txt: 'text/plain',
  tsv: 'text/tab-separated-values',
  xml: 'application/xml',
  yaml: 'text/plain',
  yml: 'text/plain',
};

const LATEX_TEXT_EXTENSIONS = new Set(['bib', 'sty', 'tex']);

function normalizePath(path: string): string {
  return path.trim().replace(/\\/g, '/').replace(/\/+/g, '/').replace(/\/$/, '');
}

function relativePathFor(path: string, root: string): string | undefined {
  const normalizedPath = normalizePath(path);
  const normalizedRoot = normalizePath(root);
  if (!normalizedRoot) return undefined;
  if (normalizedPath === normalizedRoot) return '';
  if (normalizedPath.startsWith(`${normalizedRoot}/`)) return normalizedPath.slice(normalizedRoot.length + 1);
  return undefined;
}

function baseName(path: string): string {
  const normalized = normalizePath(path);
  return normalized.slice(normalized.lastIndexOf('/') + 1) || normalized;
}

function fileExtension(name: string): string {
  const parts = name.split('.');
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
}

function mimeTypeFor(name: string): string {
  const extension = fileExtension(name);
  return MIME_BY_EXTENSION[extension] ?? 'text/plain';
}

function collectArtifactPaths(value: unknown): string[] {
  if (Array.isArray(value)) return value.flatMap((item) => collectArtifactPaths(item));
  if (!value || typeof value !== 'object') return [];

  const record = value as Record<string, unknown>;
  const paths: string[] = [];
  if (typeof record.artifact_path === 'string' && record.artifact_path.trim()) {
    paths.push(record.artifact_path);
  }
  if (Array.isArray(record.artifacts)) {
    for (const artifact of record.artifacts) {
      if (artifact && typeof artifact === 'object' && typeof (artifact as { path?: unknown }).path === 'string') {
        const path = (artifact as { path: string }).path;
        if (path.trim()) paths.push(path);
      }
    }
  }
  for (const child of Object.values(record)) {
    if (child && typeof child === 'object' && !Array.isArray(child)) {
      paths.push(...collectArtifactPaths(child));
    }
  }
  return paths;
}

export function resolveRsiArtifactSource(node: RsiTreeNode, taskId: string): RsiArtifactSource | null {
  const paths = collectArtifactPaths(node.extra);
  if (paths.length === 0) return null;
  // The canonical paper artifact is a directory. Legacy snapshots can still
  // expose a ZIP, so prefer a non-ZIP path when both representations exist.
  const path = paths.find((candidate) => fileExtension(baseName(candidate)) !== 'zip') ?? paths[0];
  return { taskId, path: normalizePath(path), initialFilePath: null };
}

export function canViewNodeArtifact(node: RsiTreeNode): boolean {
  return (node.type === 'ADOPTED' || node.type === 'REJECTED') && collectArtifactPaths(node.extra).length > 0;
}

export function isPreviewableEntry(entry: RsiArtifactFileEntry): boolean {
  if (entry.isDirectory) return false;
  const extension = fileExtension(entry.name);
  if (BINARY_EXTENSIONS.has(extension)) return false;
  if (LATEX_TEXT_EXTENSIONS.has(extension)) return true;
  if (extension === 'jsonl') return true;
  const mime = entry.mimeType ?? mimeTypeFor(entry.name);
  return mime.startsWith('text/') || mime.startsWith('image/') || mime === 'application/pdf'
    || mime === 'application/json' || mime === 'application/x-ndjson' || mime === 'application/xml';
}

export async function loadRsiArtifactFiles(source: RsiArtifactSource): Promise<RsiArtifactLoadResult> {
  const result = await rsiArtifactFilesList(source.taskId, source.path);
  const root = normalizePath(result.root);
  const entries = result.files.map((file) => ({
    name: file.name,
    path: normalizePath(file.path),
    relativePath: relativePathFor(file.path, root),
    isDirectory: file.isDirectory,
    size: file.size,
    mimeType: file.type || mimeTypeFor(file.name),
  }));
  return {
    entries,
    initialFilePath: result.initial_path ? normalizePath(result.initial_path) : null,
  };
}

export function readRsiArtifactFile(source: RsiArtifactSource, path: string): Promise<RsiArtifactFileGetResult> {
  return rsiArtifactFilesGet(source.taskId, path);
}

export function artifactMimeType(entry: RsiArtifactFileEntry): string {
  if (LATEX_TEXT_EXTENSIONS.has(fileExtension(entry.name))) return 'text/plain';
  return entry.mimeType ?? mimeTypeFor(entry.name);
}

export function rsiArtifactFileName(path: string): string {
  return baseName(path);
}
