/**
 * 将 SkillNet / GitHub 技能目录 URL 规范化为可比较形式（主机小写、去尾斜杠等）。
 * 用于搜索结果 skill_url 与本地 skills[].origin 对照。
 */
export function normalizeSkillNetUrl(raw: string): string {
  const s = raw.trim();
  if (!s) return "";
  try {
    const u = new URL(s.startsWith("http://") || s.startsWith("https://") ? s : `https://${s}`);
    if (u.hostname.toLowerCase() === "github.com") {
      u.protocol = "https:";
    }
    u.hostname = u.hostname.toLowerCase();
    let path = u.pathname;
    if (path.length > 1 && path.endsWith("/")) {
      path = path.slice(0, -1);
    }
    return `${u.origin}${path}${u.search}${u.hash}`;
  } catch {
    return s.replace(/\/$/, "").toLowerCase();
  }
}

/** ClawHub 本地 skills[].origin：有发布者时用 clawhub:owner/slug，避免同名 slug 误判已安装。 */
export function buildClawHubOrigin(slug: string, ownerHandle?: string | null): string {
  const s = String(slug || "").trim();
  const owner = String(ownerHandle || "").trim();
  if (!s) return "";
  return owner ? `clawhub:${owner}/${s}` : `clawhub:${s}`;
}

/** 判断 ClawHub 搜索结果是否已安装：优先 owner+slug，并兼容旧版 clawhub:slug / 缺 owner 时的 owner/slug。 */
export function isClawHubOriginInstalled(
  slug: string,
  ownerHandle: string | null | undefined,
  installedOrigins: ReadonlySet<string> | undefined
): boolean {
  if (!installedOrigins?.size) return false;
  const s = String(slug || "").trim();
  if (!s) return false;
  const owner = String(ownerHandle || "").trim();
  const legacyOrigin = normalizeSkillNetUrl(buildClawHubOrigin(s));
  if (owner) {
    if (installedOrigins.has(normalizeSkillNetUrl(buildClawHubOrigin(s, owner)))) return true;
    // 升级前记录可能仍是 clawhub:slug（磁盘按 slug 唯一）
    return installedOrigins.has(legacyOrigin);
  }
  if (installedOrigins.has(legacyOrigin)) return true;
  // 搜索项缺 owner 时，兼容新安装写入的 clawhub:owner/slug
  const slugCf = s.toLowerCase();
  for (const origin of installedOrigins) {
    const n = normalizeSkillNetUrl(origin);
    if (n.startsWith("clawhub:") && n.endsWith(`/${slugCf}`)) return true;
  }
  return false;
}
