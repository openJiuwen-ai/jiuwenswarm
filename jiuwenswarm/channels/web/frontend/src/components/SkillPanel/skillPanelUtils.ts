/**
 * SkillPanel 纯函数工具与常量
 *
 * 从 index.tsx 抽取，内容保持不变。
 */
import type { MarketplacePluginItem, SkillItem } from './types';

/** 广场复用技能入口；保留 Pack 类型供详情和安装使用，不按 Pack 另开分组。 */
export function groupMarketplaceSkills(skills: MarketplacePluginItem[]) {
  const teamSkills: MarketplacePluginItem[] = [];
  const featuredSkills: MarketplacePluginItem[] = [];
  for (const skill of skills) {
    if (skill.plugin_type === 'swarmskill') teamSkills.push(skill);
    else featuredSkills.push(skill);
  }
  return { teamSkills, featuredSkills };
}

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

/**
 * 将 Markdown 文件中引用的相对路径解析为技能包内文件路径。
 *
 * - 以当前预览文件所在目录为基准，支持 `./`、`../` 和 URL 编码路径；
 * - 忽略查询串与 fragment，外链、锚点及越出技能包根目录的路径返回 null；
 * - 返回路径与 `skills.files.list/get` 使用的包内相对路径保持一致。
 */
export function resolveSkillFileLinkPath(currentFilePath: string, href: string): string | null {
  const rawPath = (href || '').trim().split(/[?#]/, 1)[0];
  if (!rawPath || rawPath.startsWith('#') || rawPath.startsWith('//')) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(rawPath)) return null;

  let decodedPath = rawPath;
  try {
    decodedPath = decodeURIComponent(rawPath);
  } catch {
    // Keep malformed escape sequences literal; the backend still validates the selected path.
  }
  decodedPath = decodedPath.replace(/\\/g, '/');

  const segments = decodedPath.startsWith('/')
    ? []
    : currentFilePath.replace(/\\/g, '/').split('/').slice(0, -1);
  for (const part of decodedPath.split('/')) {
    if (!part || part === '.') continue;
    if (part === '..') {
      if (segments.length === 0) return null;
      segments.pop();
      continue;
    }
    segments.push(part);
  }
  return segments.length > 0 ? segments.join('/') : null;
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

/** 技能列表/广场卡片上的「来源」文案。
 *
 * 优先级：企业安装类型（prebuilt/user/builtin）> 明确的市场 source >
 * is_builtin_source。否则广场安装的同名技能会被误标成「内置」
 * （内置目录里也有同名包时 is_builtin_source=true）。
 */
export function getSourceLabel(
  source: string,
  t: (key: string) => string,
  isBuiltinSource?: boolean,
  sourceType?: string,
): string {
  if (sourceType === 'prebuilt') return t('skills.enterprisePrebuilt');
  if (sourceType === 'builtin') return t('skills.source.builtin');
  if (source === 'clawhub') return t('skills.source.clawhub');
  if (source === 'skillnet') return t('skills.source.skillnet');
  if (source === 'teamskillshub') return t('skills.source.teamskillshub');
  if (source === 'swarmskillhub') return t('skills.source.swarmskillhub');
  if (sourceType === 'user') {
    if (source === 'local') return t('skills.source.local');
    if (source === 'project') return t('skills.source.project');
    return source || t('skills.source.unknown');
  }
  if (isBuiltinSource) return t('skills.source.builtin');
  if (source === 'local') return t('skills.source.local');
  if (source === 'project') return t('skills.source.project');
  if (source === 'builtin') return t('skills.source.builtin');
  return source || t('skills.source.unknown');
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

/** 预置技能（source_type=prebuilt）。 */
export function isPrebuiltSkill(skill: Pick<SkillItem, 'source_type'>): boolean {
  return String(skill.source_type || '').trim() === 'prebuilt';
}

/**
 * 本地技能：磁盘扫描 / 本地导入登记（source=local，或 origin 为 local / local:…）。
 * 与广场用户自装（source_type=user 且 source 为 hub 等）区分，便于单独页签展示。
 */
export function isLocalSkill(skill: Pick<SkillItem, 'source' | 'origin'>): boolean {
  const source = String(skill.source || '').trim();
  if (source === 'local') return true;
  const origin = String(skill.origin || '').trim();
  return origin === 'local' || origin.startsWith('local:');
}

/**
 * 内置技能。广场自装（source_type=user）即使同名也不算内置；
 * 兼容旧列表仅 source=builtin 且未标成 user。
 */
export function isBuiltinSkill(skill: Pick<SkillItem, 'source_type' | 'source'>): boolean {
  const sourceType = String(skill.source_type || '').trim();
  if (sourceType === 'user' || sourceType === 'prebuilt') return false;
  if (sourceType === 'builtin') return true;
  return skill.source === 'builtin';
}

/**
 * 不展示编辑、卸载的技能（发布见 shouldShowSkillPublish）。
 * - 预置：各版本均管理员托管
 * - 内置：仅企业版托管；个人版与 origin/develop 一致，可编辑/卸载
 * （后端也会拒绝对应 uninstall；启停见 shouldShowEnableSwitch。）
 */
export function isAdminManagedSkill(
  skill: Pick<SkillItem, 'source_type' | 'source'>,
  enterpriseEdition = false,
): boolean {
  if (isPrebuiltSkill(skill)) return true;
  return enterpriseEdition && isBuiltinSkill(skill);
}

/**
 * 是否展示技能详情「发布」入口。
 * 与编辑/卸载一致：非管理员托管技能可发布（企业版仅屏蔽 prebuilt/builtin）。
 */
export function shouldShowSkillPublish(
  skill: Pick<SkillItem, 'source_type' | 'source'>,
  enterpriseEdition: boolean,
): boolean {
  return !isAdminManagedSkill(skill, enterpriseEdition);
}

/**
 * 是否展示启停开关。
 * - 企业版：预置 / 内置 / 用户技能均可
 * - 单机版：内置与用户技能可启停（与 develop 一致）；预置不展示
 */
export function shouldShowEnableSwitch(
  skill: Pick<SkillItem, 'source_type' | 'source'>,
  enterpriseEdition: boolean,
): boolean {
  if (enterpriseEdition) return true;
  return !isPrebuiltSkill(skill);
}

/**
 * 详情页当前版本展示文案。
 * 优先 product ``version``，企业版可回退账本 ``version_id``；
 * 已有前导 ``v``/``V`` 时不再加前缀，避免 ``v-1`` → ``vv-1``。
 */
export function formatSkillVersionLabel(
  skill: Pick<SkillItem, 'version' | 'version_id'>,
  options?: { allowVersionId?: boolean },
): string | null {
  const allowVersionId = Boolean(options?.allowVersionId);
  const raw = String(
    skill.version || (allowVersionId ? skill.version_id : '') || '',
  ).trim();
  if (!raw) return null;
  return /^v/i.test(raw) ? raw : `v${raw}`;
}

/** 广场搜索结果列表 key：ClawHub 带 owner，避免同 slug 重复 key。 */
export function hubMarketplaceItemKey(skill: MarketplacePluginItem): string {
  const source = skill.source || 'teamskillshub';
  const identifier = skill.identifier || skill.asset_id;
  if (source === 'clawhub' && skill.owner_handle) {
    return `${source}:${skill.owner_handle}/${identifier}`;
  }
  return `${source}:${identifier}`;
}

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
