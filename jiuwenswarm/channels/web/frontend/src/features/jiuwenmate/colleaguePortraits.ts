import commerce from '../../assets/jiuwenmate/bees/commerce.png';
import content from '../../assets/jiuwenmate/bees/content.png';
import customDefault from '../../assets/jiuwenmate/bees/custom-default.png';
import data from '../../assets/jiuwenmate/bees/data.png';
import document from '../../assets/jiuwenmate/bees/document.png';
import finance from '../../assets/jiuwenmate/bees/finance.png';
import legal from '../../assets/jiuwenmate/bees/legal.png';
import study from '../../assets/jiuwenmate/bees/study.png';
import team from '../../assets/jiuwenmate/bees/team.png';
import type { JiuwenMateCategory, JiuwenMateColleagueDraft } from './types';

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
  return colleague.avatarUrl ?? PORTRAITS[colleague.category];
}
