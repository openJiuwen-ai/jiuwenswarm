export type ColleagueCapabilityKind = 'skills' | 'connectors' | 'plugins' | 'knowledge';

export interface ColleagueCapabilitySelections {
  skills: readonly string[];
  connectors: readonly string[];
  plugins: readonly string[];
  knowledge: readonly string[];
}

export interface ColleagueCapabilityCatalog {
  skills: readonly { name: string; label?: string }[] | null;
  connectors: readonly { name: string; label?: string }[] | null;
  plugins: readonly { name: string; label?: string }[] | null;
  knowledge: readonly { name: string; label?: string }[] | null;
}

export type MutableColleagueCapabilitySelections = Record<ColleagueCapabilityKind, string[]>;

const CAPABILITY_KINDS: readonly ColleagueCapabilityKind[] = ['skills', 'connectors', 'plugins', 'knowledge'];
const INSTALLABLE_SKILL_SOURCES = new Set(['clawhub', 'skillnet', 'teamskillshub']);
const SELECTABLE_SKILL_TYPES = new Set(['skill', 'swarm_skill', 'multimodal_skill']);
const TEAM_SKILL_MARKERS = new Set([
  'swarm_skill',
  'swarm-skill',
  'swarmskill',
  'team-skill',
  'teamskills',
  'team_skill',
]);

/** Team skills belong to a group of agents, so a single employee must not be offered them. */
export function isColleagueTeamSkill(skill: {
  skillType?: string;
  kind?: string;
  pluginType?: string;
  isTeam?: boolean;
}): boolean {
  if (skill.isTeam) return true;
  return [skill.skillType, skill.kind, skill.pluginType].some((value) =>
    TEAM_SKILL_MARKERS.has((value ?? '').trim().toLocaleLowerCase()),
  );
}

function resolveCatalogOption(
  catalog: ColleagueCapabilityCatalog,
  kind: ColleagueCapabilityKind,
  selectedName: string,
): { name: string; label?: string } | null {
  const options = catalog[kind] ?? [];
  const exact = options.filter((option) => option.name === selectedName);
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return null;

  const labels = options.filter((option) => option.label === selectedName);
  return labels.length === 1 ? labels[0] : null;
}

const PUBLISHABLE_SKILL_NAME = /^[A-Za-z0-9._-]{1,128}$/;

/** Directory names of an installed expert's own skills. Publish copies these onto the employee. */
export function installedExpertSkillNames(skills: readonly { id: string; name: string }[]): string[] {
  const seen = new Set<string>();
  const names: string[] = [];
  for (const skill of skills) {
    const name = skill.id.trim() || skill.name.trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    names.push(name);
  }
  return names;
}

/**
 * An installed expert's skills ship inside its package, so they stay selected
 * even when the shared skill library does not list them.
 */
export function withInstalledExpertSkills(
  bundled: readonly string[],
  split: { available: MutableColleagueCapabilitySelections; missing: MutableColleagueCapabilitySelections },
): { available: MutableColleagueCapabilitySelections; missing: MutableColleagueCapabilitySelections } {
  const kept = new Set<string>();
  for (const name of bundled) {
    const trimmed = name.trim();
    if (PUBLISHABLE_SKILL_NAME.test(trimmed)) kept.add(trimmed);
  }
  const skills = [...split.available.skills];
  for (const name of kept) {
    if (!skills.includes(name)) skills.push(name);
  }
  return {
    available: { ...split.available, skills },
    missing: { ...split.missing, skills: split.missing.skills.filter((name) => !kept.has(name)) },
  };
}

/** Keep only template capabilities that the current host can publish. */
export function splitAvailableColleagueCapabilities(
  capabilities: ColleagueCapabilitySelections,
  catalog: ColleagueCapabilityCatalog,
): { available: MutableColleagueCapabilitySelections; missing: MutableColleagueCapabilitySelections } {
  const available = { skills: [], connectors: [], plugins: [], knowledge: [] } as MutableColleagueCapabilitySelections;
  const missing = { skills: [], connectors: [], plugins: [], knowledge: [] } as MutableColleagueCapabilitySelections;

  for (const kind of CAPABILITY_KINDS) {
    for (const name of capabilities[kind]) {
      const option = resolveCatalogOption(catalog, kind, name);
      if (!option) {
        if (!missing[kind].includes(name)) missing[kind].push(name);
      } else if (!available[kind].includes(option.name)) {
        available[kind].push(option.name);
      }
    }
  }
  return { available, missing };
}

/** Smart generation uses the existing suggestions only when they are installed and publishable. */
export function selectGeneratedColleagueCapabilities(
  generated: ColleagueCapabilitySelections,
  catalog: ColleagueCapabilityCatalog,
): MutableColleagueCapabilitySelections {
  return splitAvailableColleagueCapabilities(generated, catalog).available;
}

export interface SkillInstallCandidate {
  source: string;
  name: string;
  display_name: string;
  identifier: string;
  description?: string;
  owner_handle?: string;
}

export interface TemplateSkillRecommendations {
  status: 'idle' | 'loading' | 'ready' | 'error';
  local: readonly { name: string; label: string; description: string }[];
  plaza: readonly SkillInstallCandidate[];
  error: string | null;
}

export interface TemplateSkillSearchContext {
  name: string;
  coreAbilities?: readonly { title: string }[];
  skills?: readonly { name: string }[];
  skillSearchTerms?: readonly string[];
}

const GENERIC_SKILL_TOKENS = new Set([
  '检查',
  '练习',
  '分析',
  '规划',
  '整理',
  '评审',
  '助手',
  '教练',
  '专家',
  '顾问',
  '写作',
  '学习',
  'learning',
  'writing',
  'review',
  'analysis',
  'practice',
  'coach',
]);

function curatedSearchTerms(template: TemplateSkillSearchContext): string[] {
  return [...new Set((template.skillSearchTerms ?? []).map((value) => value.trim()).filter(Boolean))];
}

/** One plaza query per curated term. A single joined phrase matches no marketplace record. */
export function templateSkillSearchQueries(template: TemplateSkillSearchContext): string[] {
  const curated = curatedSearchTerms(template).slice(0, 8);
  if (curated.length > 0) return curated;
  const fallback = buildTemplateSkillSearchQuery(template);
  return fallback ? [fallback] : [];
}

/** Search Skills Plaza with the template's curated vocabulary. The role title is omitted because a shared word such as “教练” matches unrelated skills. */
export function buildTemplateSkillSearchQuery(template: TemplateSkillSearchContext): string {
  const curated = curatedSearchTerms(template);
  if (curated.length > 0) return curated.join(' ');
  const fallback = [
    template.name,
    ...(template.coreAbilities ?? []).map((ability) => ability.title),
    ...(template.skills ?? []).map((skill) => skill.name),
  ];
  return [...new Set(fallback.map((value) => value.trim()).filter(Boolean))].join(' ');
}

function termMatchTokens(term: string): string[] {
  const normalized = term.normalize('NFKC').toLocaleLowerCase().trim();
  if (normalized.length < 2) return [];
  const tokens = [normalized];
  const cjk = /^[\u3400-\u9fff]+$/u.test(normalized);
  if (cjk && normalized.length === 4) {
    tokens.push(normalized.slice(0, 2), normalized.slice(2));
  } else if (!cjk) {
    for (const part of normalized.split(/[^a-z0-9]+/u)) {
      if (part.length >= 4) tokens.push(part);
    }
  }
  return [...new Set(tokens)];
}

function matchesCuratedTerms(haystack: string, terms: readonly string[]): boolean {
  if (terms.length === 0) return true;
  return terms.some((term) => {
    const normalized = term.normalize('NFKC').toLocaleLowerCase();
    const tokens = termMatchTokens(term);
    const present = tokens.filter((token) => haystack.includes(token));
    if (present.includes(normalized)) return true;
    return present.some((token) => token !== normalized && !GENERIC_SKILL_TOKENS.has(token));
  });
}

/** Rank locally installed skills against template-specific terms; unrelated skills stay out of the recommendations. */
export function recommendInstalledTemplateSkills(
  terms: readonly string[],
  skills: readonly { name: string; label?: string; description?: string; skillType?: string }[],
  limit = 4,
): { name: string; label: string; description: string }[] {
  const normalizedTerms = [...new Set(terms.map((term) => term.trim().toLocaleLowerCase()).filter(Boolean))];
  const seen = new Set<string>();
  return skills
    .filter((skill) => !isColleagueTeamSkill({ skillType: skill.skillType }))
    .map((skill, index) => {
      const name = skill.name.trim();
      const label = skill.label?.trim() || name;
      const description = skill.description?.trim() || '';
      const searchable = `${name} ${label} ${description}`.normalize('NFKC').toLocaleLowerCase();
      const score = normalizedTerms.filter((term) => searchable.includes(term.normalize('NFKC'))).length;
      return { name, label, description, score, index };
    })
    .filter((skill) => {
      const key = skill.name.toLocaleLowerCase();
      if (!skill.name || skill.score === 0 || seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .slice(0, Math.max(0, limit))
    .map(({ name, label, description }) => ({ name, label, description }));
}

/** Keep installable, not-yet-installed plaza results that still contain a curated template term. */
export function recommendMarketplaceTemplateSkills(
  payload: unknown,
  installedSkillNames: readonly string[],
  limit = 5,
  terms: readonly string[] = [],
): SkillInstallCandidate[] {
  const installed = new Set(installedSkillNames.map((name) => name.trim().toLocaleLowerCase()).filter(Boolean));
  const needles = [...new Set(terms.map((term) => term.trim().normalize('NFKC').toLocaleLowerCase()).filter((term) => term.length >= 2))];
  const items = Array.isArray(object(payload).items) ? (object(payload).items as unknown[]) : [];
  const seen = new Set<string>();
  const recommendations: SkillInstallCandidate[] = [];

  for (const rawItem of items) {
    const item = object(rawItem);
    const source = string(item.source).toLocaleLowerCase();
    const name = string(item.name);
    const identifier = string(item.identifier);
    const skillType = string(item.skill_type) || string(item.kind);
    const pluginType = string(item.plugin_type);
    const displayName = string(item.display_name) || name;
    const description = string(item.description);
    const key = name.toLocaleLowerCase();
    const haystack = `${name} ${displayName} ${description}`.normalize('NFKC').toLocaleLowerCase();
    if (
      !INSTALLABLE_SKILL_SOURCES.has(source) ||
      isColleagueTeamSkill({
        skillType: string(item.skill_type),
        kind: string(item.kind),
        pluginType,
        isTeam: item.is_team_skill === true,
      }) ||
      (skillType && !SELECTABLE_SKILL_TYPES.has(skillType)) ||
      !name ||
      !identifier ||
      installed.has(key) ||
      seen.has(key) ||
      (needles.length > 0 && !matchesCuratedTerms(haystack, needles))
    ) {
      continue;
    }
    const ownerHandle = string(item.owner_handle);
    recommendations.push({
      source,
      name,
      display_name: displayName,
      identifier,
      description,
      ...(ownerHandle ? { owner_handle: ownerHandle } : {}),
    });
    seen.add(key);
    if (recommendations.length >= Math.max(0, limit)) break;
  }
  return recommendations;
}

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function string(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}
