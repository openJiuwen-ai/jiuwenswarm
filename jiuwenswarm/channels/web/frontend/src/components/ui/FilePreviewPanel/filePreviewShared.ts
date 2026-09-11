/**
 * 文件预览共享纯工具（SkillPanel 与 AgentManagementPanel 共用）
 */
import { executeDesktopSave, type DesktopSaveApiResult } from '../../../utils/desktopSave';

export type FilePreviewStatus = 'idle' | 'loading' | 'success' | 'error';

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
