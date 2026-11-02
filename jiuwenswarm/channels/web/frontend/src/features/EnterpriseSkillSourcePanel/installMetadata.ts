type SourcePresentation = {
  display_name?: string;
  version?: string;
  owner_display_name?: string;
};

export function sourceSkillIdentity(item: { source_id?: string; skill_id?: string }, fallbackSourceId = ''): string {
  return item.skill_id ? `${item.source_id || fallbackSourceId}:${item.skill_id}` : '';
}

/** 与 SkillPanel installedSkillOrigins（normalizeSkillNetUrl）对齐后再比对 */
function originKey(raw: string): string {
  const value = String(raw || '').trim();
  if (!value) return '';
  return value.replace(/\/$/, '').toLowerCase();
}

export function isSourceSkillInstalled(
  identity: string,
  origins: ReadonlySet<string> | undefined,
  loaded: boolean,
  hasUpdateStatus: boolean,
): boolean {
  if (!identity) return false;
  const key = originKey(identity);
  const matched = Boolean(
    origins?.has(identity) ||
      origins?.has(key) ||
      (key && Array.from(origins || []).some((origin) => originKey(origin) === key)),
  );
  return loaded ? matched : matched || hasUpdateStatus;
}

export function skillPresentationName(item: { name: string; market_display_name?: string; display_name?: string }): string {
  return item.market_display_name || item.display_name || item.name;
}

export function buildSourcePresentationParams(item: SourcePresentation, targetVersion?: string): Record<string, string> {
  const displayName = String(item.display_name || '').trim();
  const version = String(targetVersion || item.version || '').trim();
  const author = String(item.owner_display_name || '').trim();
  return {
    ...(displayName ? { display_name: displayName } : {}),
    ...(version ? { version } : {}),
    ...(author ? { author } : {}),
  };
}

export type LocalSkillOriginRef = {
  name: string;
  display_name?: string | null;
  origin?: string | null;
  skill_type?: string;
  /** 企业版来源类型：builtin / prebuilt / user；名称回退仅对 builtin/prebuilt 生效 */
  source_type?: string | null;
};

function isAdminManagedLocalSkill(skill: LocalSkillOriginRef): boolean {
  const sourceType = String(skill.source_type || '').trim();
  return sourceType === 'builtin' || sourceType === 'prebuilt';
}

/**
 * 解析企业广场条目对应的本地已装技能。
 * 1) 先按 origin（source_id:skill_id）精确匹配——企业源真实安装态
 * 2) 再仅对 builtin/prebuilt 做 name/display_name 回退——内置技能通常没有企业源 origin，
 *    但仍需在广场显示「去试试」（如 swarmskill-creator）
 *
 * 不对 user/广场自装/本地导入做名称回退，避免同名未装企业源条目被误判为已安装。
 */
export function resolveSourceInstalledLocalSkill(
  item: { source_id?: string; skill_id?: string; name: string; display_name?: string },
  fallbackSourceId: string,
  localSkills: readonly LocalSkillOriginRef[],
): LocalSkillOriginRef | null {
  const identity = sourceSkillIdentity(item, fallbackSourceId);
  if (identity) {
    const normalized = originKey(identity);
    for (const skill of localSkills) {
      const origin = String(skill.origin || '').trim();
      if (!origin) continue;
      if (origin === identity || originKey(origin) === normalized) {
        return skill;
      }
    }
  }

  const candidates = [item.name, item.display_name]
    .map((value) => String(value || '').trim())
    .filter(Boolean);
  const managed = localSkills.filter(isAdminManagedLocalSkill);
  for (const candidate of candidates) {
    const byName = managed.find((skill) => skill.name === candidate);
    if (byName) return byName;
    const byDisplay = managed.find(
      (skill) => String(skill.display_name || '').trim() === candidate,
    );
    if (byDisplay) return byDisplay;
  }
  return null;
}

export function isEnterpriseSourceSkillInstalled(
  item: { source_id?: string; skill_id?: string; name: string; display_name?: string },
  fallbackSourceId: string,
  localSkills: readonly LocalSkillOriginRef[],
  installedOrigins: ReadonlySet<string> | undefined,
  installedStateLoaded: boolean,
  hasUpdateStatus: boolean,
): boolean {
  if (resolveSourceInstalledLocalSkill(item, fallbackSourceId, localSkills)) {
    return true;
  }
  return isSourceSkillInstalled(
    sourceSkillIdentity(item, fallbackSourceId),
    installedOrigins,
    installedStateLoaded,
    hasUpdateStatus,
  );
}
