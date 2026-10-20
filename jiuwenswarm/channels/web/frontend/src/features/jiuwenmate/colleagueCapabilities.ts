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
  coreAbilities: readonly { title: string }[];
  skillSearchTerms?: readonly string[];
}

/** Build a short Skills Plaza query from the selected template's role and skills vocabulary. */
export function buildTemplateSkillSearchQuery(template: TemplateSkillSearchContext): string {
  const values = [
    template.name,
    ...template.coreAbilities.map((ability) => ability.title),
    ...(template.skillSearchTerms ?? []),
  ];
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))].join(' ');
}

/** Rank locally installed skills against template-specific terms; unrelated skills stay out of the recommendations. */
export function recommendInstalledTemplateSkills(
  terms: readonly string[],
  skills: readonly { name: string; label?: string; description?: string }[],
  limit = 4,
): { name: string; label: string; description: string }[] {
  const normalizedTerms = [...new Set(terms.map((term) => term.trim().toLocaleLowerCase()).filter(Boolean))];
  const seen = new Set<string>();
  return skills
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

/** Keep installable, not-yet-installed plaza results in the order returned by search. */
export function recommendMarketplaceTemplateSkills(
  payload: unknown,
  installedSkillNames: readonly string[],
  limit = 5,
): SkillInstallCandidate[] {
  const installed = new Set(installedSkillNames.map((name) => name.trim().toLocaleLowerCase()).filter(Boolean));
  const items = Array.isArray(object(payload).items) ? (object(payload).items as unknown[]) : [];
  const seen = new Set<string>();
  const recommendations: SkillInstallCandidate[] = [];

  for (const rawItem of items) {
    const item = object(rawItem);
    const source = string(item.source).toLocaleLowerCase();
    const name = string(item.name);
    const identifier = string(item.identifier);
    const skillType = string(item.skill_type);
    const key = name.toLocaleLowerCase();
    if (
      !INSTALLABLE_SKILL_SOURCES.has(source) ||
      (skillType && !SELECTABLE_SKILL_TYPES.has(skillType)) ||
      !name ||
      !identifier ||
      installed.has(key) ||
      seen.has(key)
    ) {
      continue;
    }
    const displayName = string(item.display_name) || name;
    const ownerHandle = string(item.owner_handle);
    recommendations.push({
      source,
      name,
      display_name: displayName,
      identifier,
      description: string(item.description),
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
