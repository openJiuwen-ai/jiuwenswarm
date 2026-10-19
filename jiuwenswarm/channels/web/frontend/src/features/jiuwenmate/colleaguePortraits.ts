import commerce from '../../assets/jiuwenmate/bees/commerce.png';
import content from '../../assets/jiuwenmate/bees/content.png';
import customDefault from '../../assets/jiuwenmate/bees/custom-default.png';
import data from '../../assets/jiuwenmate/bees/data.png';
import document from '../../assets/jiuwenmate/bees/document.png';
import finance from '../../assets/jiuwenmate/bees/finance.png';
import legal from '../../assets/jiuwenmate/bees/legal.png';
import study from '../../assets/jiuwenmate/bees/study.png';
import team from '../../assets/jiuwenmate/bees/team.png';
import { CUSTOM_AVATAR_HUES, readAvatarHue } from './avatarHue';
import type { JiuwenMateCategory, JiuwenMateColleagueDraft } from './types';

export { CUSTOM_AVATAR_HUES };

const PORTRAITS: Record<JiuwenMateCategory, string> = {
  education: study,
  development: team,
  data,
  document,
  finance,
  legal,
  commerce,
  content,
  custom: customDefault,
};

export function categoryPortrait(category: JiuwenMateCategory): string {
  return PORTRAITS[category];
}

export function colleaguePortrait(colleague: Pick<JiuwenMateColleagueDraft, 'category' | 'avatarUrl'>): string {
  if (!colleague.avatarUrl || readAvatarHue(colleague.avatarUrl) !== null) return PORTRAITS[colleague.category];
  return colleague.avatarUrl;
}

export function customAvatarHue(colleague: {
  id?: string;
  category: JiuwenMateCategory;
  avatarUrl?: string;
  avatarHue?: number;
  isDefault?: boolean;
}): number | null {
  if (colleague.category !== 'custom' || colleague.isDefault) return null;
  if (colleague.avatarUrl && readAvatarHue(colleague.avatarUrl) === null) return null;
  if ((CUSTOM_AVATAR_HUES as readonly number[]).includes(colleague.avatarHue ?? -1)) return colleague.avatarHue ?? null;
  const stored = readAvatarHue(colleague.avatarUrl);
  if (stored !== null) return stored;
  if (!colleague.id) return null;
  let hash = 0;
  for (let index = 0; index < colleague.id.length; index += 1) {
    hash = (Math.imul(hash, 33) + colleague.id.charCodeAt(index)) >>> 0;
  }
  return CUSTOM_AVATAR_HUES[hash % CUSTOM_AVATAR_HUES.length];
}
