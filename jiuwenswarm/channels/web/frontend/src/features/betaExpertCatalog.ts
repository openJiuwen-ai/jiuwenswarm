export interface BetaExpertSkill {
  name: string;
  description: string;
}

export interface BetaExpertMember {
  id: string;
  name: string;
  description: string;
  role: string;
}

export interface BetaExpertCallChainStep {
  name: string;
  description: string;
}

export interface BetaExpertCatalogItem {
  id: string;
  name: string;
  description: string;
  source: string;
  available: boolean;
  type: string;
  tags: string[];
  skills: BetaExpertSkill[];
  members: BetaExpertMember[];
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
    .map(item => {
      if (!item || typeof item !== 'object') return null;
      const record = item as Record<string, unknown>;
      const name = text(record.name);
      if (!name) return null;
      return { name, description: text(record.description) };
    })
    .filter((item): item is BetaExpertSkill => item !== null);
}

function normalizeMembers(value: unknown): BetaExpertMember[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(item => {
      if (!item || typeof item !== 'object') return null;
      const record = item as Record<string, unknown>;
      const id = text(record.id);
      if (!id) return null;
      return {
        id,
        name: text(record.name) || id,
        description: text(record.description),
        role: text(record.role),
      };
    })
    .filter((item): item is BetaExpertMember => item !== null);
}

export function buildBetaExpertCallChain(expert: BetaExpertCatalogItem): BetaExpertCallChainStep[] {
  if (expert.type === 'team') {
    const workers = expert.members.filter(member => member.role !== 'lead');
    if (expert.workflow.length > 0) {
      return expert.workflow.map((description, index) => ({
        name: workers[index]?.name || `协作阶段 ${index + 1}`,
        description,
      }));
    }
    return expert.members.map(member => ({
      name: member.name,
      description: member.description || (member.role === 'lead' ? '理解任务、调度成员并汇总最终成品' : '完成对应专业阶段'),
    }));
  }
  return expert.skills.map((skill, index) => ({
    name: skill.name,
    description: expert.workflow[index] || skill.description,
  }));
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
      const metadata = expert.metadata && typeof expert.metadata === 'object' ? (expert.metadata as Record<string, unknown>) : {};
      const summarizedMembers = normalizeMembers(expert.members);
      const members =
        summarizedMembers.length > 0
          ? summarizedMembers
          : textList(metadata.memberExpertIds).map(memberId => ({ id: memberId, name: memberId, description: '', role: 'member' }));
      const rawType = text(expert.type);
      return {
        id,
        name: text(expert.name) || id,
        description: text(expert.description),
        source: text(expert.source),
        available: expert.available === true,
        type: rawType === 'agent_group' || (!rawType && members.length > 0) ? 'team' : rawType || 'agent',
        tags: textList(expert.tags),
        skills: normalizeSkills(expert.skills),
        members,
        profession: text(metadata.profession),
        quickPrompts: textList(metadata.quickPrompts),
        audiences: textList(metadata.audiences),
        deliverables: textList(metadata.deliverables),
        workflow: textList(metadata.workflow ?? expert.workflow),
      };
    })
    .filter((item): item is BetaExpertCatalogItem => item !== null);
}
