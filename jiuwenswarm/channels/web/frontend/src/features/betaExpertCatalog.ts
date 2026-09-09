export interface BetaExpertSkill {
  name: string;
  description: string;
}

export interface BetaExpertCatalogItem {
  id: string;
  name: string;
  description: string;
  source: string;
  available: boolean;
  tags: string[];
  skills: BetaExpertSkill[];
  profession: string;
  quickPrompts: string[];
  audiences: string[];
  deliverables: string[];
  workflow: string[];
}

type RawExpert = Record<string, unknown>;

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function textList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(text).filter(Boolean);
}

function normalizeSkills(value: unknown): BetaExpertSkill[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (!item || typeof item !== 'object') return null;
      const record = item as Record<string, unknown>;
      const name = text(record.name);
      if (!name) return null;
      return { name, description: text(record.description) };
    })
    .filter((item): item is BetaExpertSkill => item !== null);
}

export function normalizeBetaExpertCatalog(payload: unknown): BetaExpertCatalogItem[] {
  if (!payload || typeof payload !== 'object') return [];
  const rawExperts = (payload as { experts?: unknown }).experts;
  if (!Array.isArray(rawExperts)) return [];

  return rawExperts
    .map((raw): BetaExpertCatalogItem | null => {
      if (!raw || typeof raw !== 'object') return null;
      const expert = raw as RawExpert;
      const id = text(expert.id);
      if (!id) return null;
      const metadata = expert.metadata && typeof expert.metadata === 'object'
        ? expert.metadata as Record<string, unknown>
        : {};
      return {
        id,
        name: text(expert.name) || id,
        description: text(expert.description),
        source: text(expert.source),
        available: expert.available === true,
        tags: textList(expert.tags),
        skills: normalizeSkills(expert.skills),
        profession: text(metadata.profession),
        quickPrompts: textList(metadata.quickPrompts),
        audiences: textList(metadata.audiences),
        deliverables: textList(metadata.deliverables),
        workflow: textList(metadata.workflow),
      };
    })
    .filter((item): item is BetaExpertCatalogItem => item !== null);
}
