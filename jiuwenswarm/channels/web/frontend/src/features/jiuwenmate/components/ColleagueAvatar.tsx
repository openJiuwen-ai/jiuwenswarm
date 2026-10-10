import { colleaguePortrait } from '../colleaguePortraits';
import type { JiuwenMateColleagueDraft } from '../types';

interface ColleagueAvatarProps {
  colleague: Pick<JiuwenMateColleagueDraft, 'name' | 'category' | 'avatarUrl'> & { id?: string };
  size?: 'small' | 'medium' | 'large';
}

export function ColleagueAvatar({ colleague, size = 'medium' }: ColleagueAvatarProps) {
  return (
    <span
      className={`jiuwenmate-avatar jiuwenmate-avatar--${size}`}
      data-category={colleague.category}
      data-testid="jiuwenmate-colleague-avatar"
      data-variant={colleague.id ?? colleague.category}
      aria-label={colleague.name}
    >
      <img src={colleaguePortrait(colleague)} alt="" />
    </span>
  );
}
