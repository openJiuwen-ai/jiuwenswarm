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

/** 广场 / 在线搜索条目：用 origin 反查本地技能名（SKILL.md name 可能 ≠ slug）。 */
export type MarketplaceInstallRef = {
  source?: string | null;
  name: string;
  identifier?: string | null;
  asset_id?: string;
  owner_handle?: string | null;
};

export type LocalSkillOriginRef = {
  name: string;
  origin?: string | null;
};

/**
 * 解析广场技能对应的本地已安装技能名。
 * ClawHub / TeamSkillsHub / SkillNet 优先按 origin 匹配；再回退到 name。
 */
export function resolveMarketplaceInstalledLocalName(
  skill: MarketplaceInstallRef,
  localSkills: readonly LocalSkillOriginRef[],
  installedNames?: ReadonlySet<string>,
): string | null {
  const source = String(skill.source || "teamskillshub").trim().toLowerCase();
  const identifier = String(skill.identifier || skill.asset_id || skill.name || "").trim();

  if (source === "clawhub") {
    const slug = identifier || skill.name;
    const owner = String(skill.owner_handle || "").trim();
    const slugCf = slug.toLowerCase();
    for (const local of localSkills) {
      const origin = local.origin?.trim();
      if (!origin) continue;
      const n = normalizeSkillNetUrl(origin);
      if (!n.startsWith("clawhub:")) continue;
      if (owner) {
        if (n === normalizeSkillNetUrl(buildClawHubOrigin(slug, owner))) return local.name;
        // 兼容旧版 clawhub:slug
        if (n === normalizeSkillNetUrl(buildClawHubOrigin(slug))) return local.name;
      } else if (n === normalizeSkillNetUrl(buildClawHubOrigin(slug))) {
        return local.name;
      } else if (n.endsWith(`/${slugCf}`)) {
        return local.name;
      }
    }
  } else if (source === "teamskillshub") {
    const hubOrigin = normalizeSkillNetUrl(`teamskillshub:${identifier}`);
    for (const local of localSkills) {
      const origin = local.origin?.trim();
      if (origin && normalizeSkillNetUrl(origin) === hubOrigin) return local.name;
    }
  } else if (source === "skillnet") {
    const url = normalizeSkillNetUrl(identifier);
    for (const local of localSkills) {
      const origin = local.origin?.trim();
      if (origin && normalizeSkillNetUrl(origin) === url) return local.name;
    }
  }

  if (installedNames?.has(skill.name)) return skill.name;
  return null;
}

export function isMarketplaceSkillInstalled(
  skill: MarketplaceInstallRef,
  localSkills: readonly LocalSkillOriginRef[],
  installedNames?: ReadonlySet<string>,
): boolean {
  return resolveMarketplaceInstalledLocalName(skill, localSkills, installedNames) != null;
}
