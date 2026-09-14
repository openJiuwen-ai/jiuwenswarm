/**
 * SkillPanel 纯函数工具与常量
 *
 * 从 index.tsx 抽取，内容保持不变。
 */
import type { SkillItem } from './types';

/** 刷新会 git pull marketplace，略放宽；普通进页单次 RPC 一般很快。 */
export const SKILLS_FETCH_TIMEOUT_REFRESH_MS = 60_000;
export const SKILLS_FETCH_TIMEOUT_NORMAL_MS = 30_000;
export const GRAPH_READING_MIN_VISIBLE_MS = 500;

// ── 发布表单校验（与 skillhub 对齐） ──
// 技能名：小写字母开头，小写字母/数字/连字符，最长 64
export const SKILL_NAME_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
export const SKILL_NAME_MAX_LEN = 64;
// 版本号：严格 x.y.z 三段数字
export const VERSION_PATTERN = /^[0-9]+\.[0-9]+\.[0-9]+$/;
// 显示名：非空，最长 128
export const DISPLAY_NAME_MAX_LEN = 128;

const PREVIEWABLE_MIME_TYPES = ['application/json', 'application/xml', 'application/javascript', 'application/x-yaml'];
const PREVIEWABLE_FILE_EXTS = ['md', 'mdx', 'json', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];

/** 判断文件是否可预览（纯函数，供文件树使用） */
export function isFilePreviewable(entry: { type: string; mime_type: string | null; name: string }): boolean {
  if (entry.type !== 'file') return false;
  const mime = entry.mime_type || '';
  if (mime.startsWith('text/')) return true;
  if (mime.startsWith('image/')) return true;
  if (PREVIEWABLE_MIME_TYPES.includes(mime)) return true;
  const ext = entry.name.split('.').pop()?.toLowerCase() || '';
  return PREVIEWABLE_FILE_EXTS.includes(ext);
}

/** 与后端一致：tags/allowed_tools 可能是逗号分隔字符串，统一为 string[] */
export function coerceStringList(val: unknown): string[] {
  if (val == null) return [];
  if (Array.isArray(val)) {
    return val.map((x) => String(x).trim()).filter(Boolean);
  }
  if (typeof val === 'string') {
    const s = val.trim();
    if (!s) return [];
    return s.includes(',')
      ? s
          .split(',')
          .map((p) => p.trim())
          .filter(Boolean)
      : [s];
  }
  return [String(val)];
}

export function normalizeSkillItem<T extends SkillItem>(raw: T): T {
  return {
    ...raw,
    tags: coerceStringList(raw.tags),
    allowed_tools: coerceStringList(raw.allowed_tools),
  };
}

export const MARKETPLACE_CATEGORIES = [
  'all',
  'software-development',
  'office-productivity',
  'content-creation',
  'multimodal-media',
  'data-science-research',
  'compliance-legal',
  'lifestyle-health',
  'finance-wealth',
] as const;

/**
 * 将技能内容中的图片路径转换为 /file-api/raw-file 可访问的 URL。
 * 处理两种情况：
 * 1. 相对路径（references/img_00.png）→ 直接拼接技能目录
 * 2. 后端改写的 /file-api/download?token=xxx → 解码 token 获取 relative_path
 * skillFilePath 为 SKILL.md 的绝对路径，用于推导技能所在目录。
 */
export function transformSkillContentImages(content: string, skillFilePath: string): string {
  if (!content || !skillFilePath) return content;
  const lastSlash = Math.max(skillFilePath.lastIndexOf('/'), skillFilePath.lastIndexOf('\\'));
  const skillDir = lastSlash >= 0 ? skillFilePath.substring(0, lastSlash).replace(/\\/g, '/') : '';

  const toRawFileUrl = (relativePath: string): string => {
    const fullPath = skillDir ? `${skillDir}/${relativePath}`.replace(/\\/g, '/') : relativePath;
    return `/file-api/raw-file?path=${encodeURIComponent(fullPath)}`;
  };

  return content.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, imgPath: string) => {
    // 跳过外部 URL 和 data URI
    if (/^(https?:|data:|file:\/\/\/)/.test(imgPath)) return match;

    // 处理后端改写的 /file-api/download?token=xxx URL
    if (imgPath.startsWith('/file-api/download?token=')) {
      try {
        const token = new URL(imgPath, 'http://localhost').searchParams.get('token') || '';
        const payloadB64 = token.split('.')[0];
        const payload = JSON.parse(atob(payloadB64));
        if (payload.relative_path) {
          return `![${alt}](${toRawFileUrl(payload.relative_path)})`;
        }
      } catch {
        // 解码失败，保留原 URL
      }
      return match;
    }

    // 处理相对路径
    if (imgPath.startsWith('/file-api/')) return match;
    return `![${alt}](${toRawFileUrl(imgPath)})`;
  });
}
