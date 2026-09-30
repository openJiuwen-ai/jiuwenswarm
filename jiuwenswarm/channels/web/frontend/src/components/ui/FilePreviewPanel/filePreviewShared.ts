/**
 * 文件预览共享纯工具（SkillPanel 与 AgentManagementPanel 共用）
 */
import { executeDesktopSave, type DesktopSaveApiResult } from '../../../utils/desktopSave';

export type FilePreviewStatus = 'idle' | 'loading' | 'success' | 'error';

/** 文件类型图标：与 src/assets/file-preview/*.svg 一一对应 */
export type FilePreviewIconType =
  | 'code'
  | 'table'
  | 'archive'
  | 'data'
  | 'video'
  | 'image'
  | 'unknown'
  | 'text'
  | 'audio'
  | 'ppt'
  | 'word'
  | 'markdown';

const CODE_FILE_PATTERN =
  /\.(?:bash|c|cc|cfg|conf|cpp|css|env|go|h|hpp|html?|ini|ipynb|java|js|json|jsx|mjs|php|py|pyw|rb|rs|sh|sql|swift|toml|ts|tsx|vue|xml|yaml|yml)$/i;
const IMAGE_FILE_PATTERN = /\.(?:png|jpe?g|gif|webp|svg|bmp)$/i;
const MARKDOWN_FILE_PATTERN = /\.mdx?$/i;

/** 取路径最后一段作为展示名（忽略结尾的 /） */
export function getPreviewFileLabel(path: string): string {
  return path.replace(/\/$/, '').split('/').filter(Boolean).pop() || path;
}

export function isCodeFileName(fileName: string): boolean {
  return CODE_FILE_PATTERN.test(fileName);
}

export function isPreviewableImagePath(path: string): boolean {
  return IMAGE_FILE_PATTERN.test(path);
}

export function isMarkdownFilePath(path: string): boolean {
  return MARKDOWN_FILE_PATTERN.test(path);
}

export function isPythonFilePath(path: string): boolean {
  return /\.py$/i.test(path);
}

export function isJsonFilePath(path: string): boolean {
  return /\.json$/i.test(path);
}

export function isPdfFilePath(path: string): boolean {
  return /\.pdf$/i.test(path);
}

/** 拆分 Markdown front matter（--- 包裹的头部），返回原文片段与正文 */
export function splitMarkdownFrontMatter(content: string): { frontMatter: string | null; body: string } {
  const match = /^(?:\uFEFF)?---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(content);
  if (!match) return { frontMatter: null, body: content };
  return { frontMatter: match[1], body: content.slice(match[0].length) };
}

/** JSON 美化（非法 JSON 时原样返回） */
export function formatJsonContent(content: string): string {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return content;
  }
}

/**
 * Clipboard text for the file-preview Copy button.
 * JSON copies the pretty-printed view shown in the panel; other types copy the source.
 */
export function getPreviewCopyText(path: string, content: string): string {
  return isJsonFilePath(path) ? formatJsonContent(content) : content;
}

/**
 * 下载预览文件：优先走 pywebview 桌面接口（download_file），否则回退浏览器 <a> 下载。
 * - downloadUrl：二进制/图片等由后端提供的下载地址
 * - content：文本内容，桌面端会转为 dataURL 保存
 * 返回是否成功（失败时由调用方提示）。
 */
export async function downloadPreviewFile(params: {
  filename: string;
  content?: string | null;
  downloadUrl?: string | null;
}): Promise<boolean> {
  const { filename, content, downloadUrl } = params;
  const downloadFileApi = window.pywebview?.api?.download_file;
  if (downloadFileApi) {
    try {
      let url = downloadUrl || null;
      if (!url && content != null) {
        const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
        url = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.onerror = () => reject(reader.error);
          reader.readAsDataURL(blob);
        });
      }
      if (!url) return false;
      const outcome = await executeDesktopSave(() => downloadFileApi(url, filename) as DesktopSaveApiResult);
      return outcome !== 'failed';
    } catch {
      return false;
    }
  }
  const triggerBrowserDownload = (href: string) => {
    const link = document.createElement('a');
    link.href = href;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };
  if (downloadUrl) {
    triggerBrowserDownload(downloadUrl);
    return true;
  }
  if (content == null) return false;
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = window.URL.createObjectURL(blob);
  triggerBrowserDownload(url);
  window.URL.revokeObjectURL(url);
  return true;
}

// ── 文件类型图标映射 ────────────────────────────────────────────

// 复合扩展名优先匹配（.tar.gz 等按整体识别为压缩包）
const COMPOUND_EXTENSION_TO_ICON: Readonly<Record<string, FilePreviewIconType>> = Object.freeze({
  '.tar.gz': 'archive',
  '.tar.bz2': 'archive',
  '.tar.xz': 'archive',
  '.tar.zst': 'archive',
});

// 按扩展名分组；同名扩展名以先出现者为准
const EXTENSION_GROUPS: ReadonlyArray<readonly [FilePreviewIconType, readonly string[]]> = [
  ['markdown', ['md', 'markdown', 'mdx']],
  // pdf 归入 PPT 图标（设计约定）
  ['ppt', ['ppt', 'pptx', 'odp', 'pdf']],
  ['word', ['doc', 'docx', 'rtf', 'odt']],
  ['table', ['xls', 'xlsx', 'csv', 'tsv', 'ods']],
  ['image', ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'ico', 'jfif', 'tif', 'tiff', 'avif', 'heic']],
  ['audio', ['mp3', 'wav', 'flac', 'aac', 'ogg', 'm4a', 'wma', 'opus', 'aiff']],
  ['video', ['mp4', 'avi', 'mov', 'mkv', 'webm', 'wmv', 'flv', 'm4v', 'mpg', 'mpeg', '3gp']],
  ['archive', ['zip', 'rar', '7z', 'tar', 'gz', 'tgz', 'bz2', 'xz', 'zst', 'lz', 'lzma', 'cab']],
  ['data', ['json', 'xml', 'yaml', 'yml', 'db', 'sqlite', 'sqlite3', 'sql', 'toml', 'ini', 'env', 'cfg', 'conf', 'properties']],
  ['text', ['txt', 'log', 'text', 'nfo', 'rst']],
  [
    'code',
    [
      'html',
      'htm',
      'xhtml',
      'py',
      'pyw',
      'ipynb',
      'js',
      'jsx',
      'mjs',
      'cjs',
      'ts',
      'tsx',
      'vue',
      'svelte',
      'java',
      'c',
      'cc',
      'cpp',
      'cxx',
      'h',
      'hpp',
      'hh',
      'cs',
      'go',
      'rs',
      'rb',
      'php',
      'swift',
      'kt',
      'kts',
      'scala',
      'sh',
      'bash',
      'zsh',
      'fish',
      'ps1',
      'bat',
      'cmd',
      'css',
      'scss',
      'sass',
      'less',
      'lua',
      'pl',
      'pm',
      'r',
      'dart',
      'gradle',
      'groovy',
      'clj',
      'ex',
      'exs',
      'erl',
      'hs',
      'ml',
      'nim',
      'zig',
      'make',
      'cmake',
    ],
  ],
];

const EXTENSION_TO_ICON: Readonly<Record<string, FilePreviewIconType>> = Object.freeze(
  Object.fromEntries(EXTENSION_GROUPS.flatMap(([iconType, extensions]) => extensions.map((extension) => [extension, iconType]))) as Record<string, FilePreviewIconType>,
);

// 无扩展名或特殊命名（Dockerfile / 各类 dotfile）按整名匹配
const FILE_NAME_TO_ICON: Readonly<Record<string, FilePreviewIconType>> = Object.freeze({
  dockerfile: 'code',
  makefile: 'code',
  'cmakelists.txt': 'code',
  '.gitignore': 'data',
  '.gitattributes': 'data',
  '.dockerignore': 'data',
  '.editorconfig': 'data',
  '.npmrc': 'data',
  '.prettierrc': 'data',
  '.eslintrc': 'data',
  '.babelrc': 'data',
});

/**
 * 由文件名解析文件类型图标。仅看文件名/扩展名，不读取内容或 MIME。
 * 未知类型统一回退到 unknown（文件未知图标）。
 */
export function resolveFilePreviewIconType(fileName: string): FilePreviewIconType {
  const name = getPreviewFileLabel(fileName).toLowerCase();
  if (!name) return 'unknown';

  for (const [extension, iconType] of Object.entries(COMPOUND_EXTENSION_TO_ICON)) {
    if (name.endsWith(extension)) return iconType;
  }

  const special = FILE_NAME_TO_ICON[name];
  if (special) return special;

  const dotIndex = name.lastIndexOf('.');
  if (dotIndex >= 0 && dotIndex < name.length - 1) {
    const iconType = EXTENSION_TO_ICON[name.slice(dotIndex + 1)];
    if (iconType) return iconType;
  }

  return 'unknown';
}
