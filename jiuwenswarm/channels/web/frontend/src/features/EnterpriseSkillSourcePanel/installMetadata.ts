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
};

/**
 * 解析企业广场条目对应的本地已装技能。
 * 对齐单机版 resolveMarketplaceInstalledLocalName：先按 origin（source_id:skill_id），再回退 name/display_name。
 * 内置技能（如 swarmskill-creator / 蜂群技能自动生成专家）通常没有企业源 origin，必须靠名称回退才能显示「去试试」。
 */
export function resolveSourceInstalledLocalSkill(
  item: { source_id?: string; skill_id?: string; name: string; display_name?: string },
  fallbackSourceId: string,
  localSkills: readonly LocalSkillOriginRef[],
  installedNames?: ReadonlySet<string>,
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
  for (const candidate of candidates) {
    const byName = localSkills.find((skill) => skill.name === candidate);
    if (byName) return byName;
    const byDisplay = localSkills.find(
      (skill) => String(skill.display_name || '').trim() === candidate,
    );
    if (byDisplay) return byDisplay;
    if (installedNames?.has(candidate)) return { name: candidate };
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
  installedNames?: ReadonlySet<string>,
): boolean {
  if (resolveSourceInstalledLocalSkill(item, fallbackSourceId, localSkills, installedNames)) {
    return true;
  }
  return isSourceSkillInstalled(
    sourceSkillIdentity(item, fallbackSourceId),
    installedOrigins,
    installedStateLoaded,
    hasUpdateStatus,
  );
}
