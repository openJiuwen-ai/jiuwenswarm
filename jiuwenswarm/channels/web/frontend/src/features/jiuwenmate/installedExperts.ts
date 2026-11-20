import { CUSTOM_AVATAR_HUES, pickAvatarHue } from './avatarHue';
import { installedExpertSkillNames } from './colleagueCapabilities';
import { customAvatarHue } from './colleaguePortraits';
import type { JiuwenMateBoundItem, JiuwenMateTemplate } from './types';

export interface InstalledExpertCard {
  id: string;
  runtimePackageName: string;
  displayName: string;
  description: string;
  installed: boolean;
  tags: readonly { id: string; label: string }[];
}

export interface InstalledExpertDetail {
  displayName?: string;
  description?: string;
  persona?: string;
  details?: string;
  tags?: readonly { id: string; label: string }[];
  skills?: readonly JiuwenMateBoundItem[];
  tools?: readonly JiuwenMateBoundItem[];
  rails?: readonly JiuwenMateBoundItem[];
  mcps?: readonly JiuwenMateBoundItem[];
  suggestedPrompts?: readonly string[];
}

export function installedExpertPackageName(item: Pick<InstalledExpertCard, 'id' | 'runtimePackageName'>): string {
  return item.runtimePackageName.trim() || item.id.trim();
}

export function installedExpertTemplateId(packageName: string): string {
  return `installed:${packageName}`;
}

/** Palette shift of the shared custom bee. The same package keeps the same color. */
export function installedExpertHue(packageName: string): number {
  return customAvatarHue({ id: packageName, category: 'custom' }) ?? CUSTOM_AVATAR_HUES[0];
}

function labels(tags: readonly { label: string }[] | undefined): string[] {
  return (tags ?? []).map((tag) => tag.label.trim()).filter((label) => label.length > 0);
}

/** An installed expert becomes a create-page card. Uninstalled experts stay out. The portrait is the custom bee. */
export function installedExpertToTemplate(
  item: InstalledExpertCard,
  detail?: InstalledExpertDetail | null,
): JiuwenMateTemplate | null {
  if (!item.installed) return null;
  const packageName = installedExpertPackageName(item);
  if (!packageName) return null;
  const tags = labels(detail?.tags ?? item.tags);
  const skills = detail?.skills ?? [];
  return {
    id: installedExpertTemplateId(packageName),
    installedPackageName: packageName,
    name: (detail?.displayName || item.displayName).trim() || packageName,
    description: (detail?.description || item.description).trim(),
    persona: (detail?.persona || detail?.details || '').trim(),
    tags,
    category: 'custom',
    avatarHue: installedExpertHue(packageName),
    skills,
    tools: detail?.tools ?? [],
    rails: detail?.rails ?? [],
    mcps: detail?.mcps ?? [],
    skillSearchTerms: [...skills.map((skill) => skill.name), ...tags],
    capabilities: {
      model: '',
      skills: installedExpertSkillNames(skills),
      connectors: [],
      plugins: [],
      knowledge: [],
      starterPrompts: [...(detail?.suggestedPrompts ?? [])],
    },
  };
}

export async function loadInstalledExpertTemplates(
  listInstalled: () => Promise<readonly InstalledExpertCard[]>,
  show: (id: string) => Promise<InstalledExpertDetail>,
): Promise<JiuwenMateTemplate[]> {
  const seen = new Set<string>();
  const installedOnce = (await listInstalled()).filter((item) => {
    if (!item.installed) return false;
    const packageName = installedExpertPackageName(item);
    if (!packageName || seen.has(packageName)) return false;
    seen.add(packageName);
    return true;
  });
  const templates = await Promise.all(
    installedOnce.map(async (item) => {
      try {
        return installedExpertToTemplate(item, await show(item.id));
      } catch {
        return installedExpertToTemplate(item);
      }
    }),
  );
  return assignInstalledExpertHues(
    templates.filter((template): template is JiuwenMateTemplate => template !== null),
    [],
  );
}

/** Give each visible expert a palette color the office is not already using. */
export function assignInstalledExpertHues(
  templates: readonly JiuwenMateTemplate[],
  usedHues: readonly number[],
): JiuwenMateTemplate[] {
  const used = usedHues.filter((hue) => (CUSTOM_AVATAR_HUES as readonly number[]).includes(hue));
  return templates.map((template) => {
    const avatarHue = pickAvatarHue(used);
    used.push(avatarHue);
    return { ...template, avatarHue };
  });
}

/** True when this template or expert already has an employee, so creation should stop. */
export function templateAlreadyCreated(
  draft: { sourceTemplateId?: string; sourceTemplateName?: string; category?: string },
  colleagues: readonly { name: string; category?: string; sourceTemplateId?: string; isDefault?: boolean }[],
): boolean {
  const source = draft.sourceTemplateId?.trim() ?? '';
  const name = draft.sourceTemplateName?.trim() ?? '';
  if (!source) return false;
  return colleagues.some((colleague) => {
    if (colleague.isDefault) return false;
    const recorded = colleague.sourceTemplateId?.trim() ?? '';
    if (recorded === source) return true;
    if (recorded || !name || colleague.name.trim() !== name) return false;
    if (draft.category === 'custom') return colleague.category === 'custom';
    return colleague.category === draft.category;
  });
}

export function isInstalledExpertHue(hue: number | undefined): boolean {
  return hue !== undefined && (CUSTOM_AVATAR_HUES as readonly number[]).includes(hue);
}
