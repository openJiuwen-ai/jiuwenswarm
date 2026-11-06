import { colleaguePortrait, customAvatarHue } from '../colleaguePortraits';
import type { JiuwenMateColleagueDraft } from '../types';

interface ColleagueAvatarProps {
  colleague: Pick<JiuwenMateColleagueDraft, 'name' | 'category' | 'avatarUrl' | 'avatarHue'> & {
    id?: string;
    isDefault?: boolean;
  };
  size?: 'small' | 'medium' | 'large';
}

export function ColleagueAvatar({ colleague, size = 'medium' }: ColleagueAvatarProps) {
  const hue = customAvatarHue(colleague);
  return (
    <span
      className={`jiuwenmate-avatar jiuwenmate-avatar--${size}`}
      data-category={colleague.category}
      data-hue={hue ?? undefined}
      data-testid="jiuwenmate-colleague-avatar"
      data-variant={colleague.id ?? colleague.category}
      style={hue === null ? undefined : { ['--jm-avatar-hue' as string]: `${hue}deg` }}
      aria-label={colleague.name}
    >
      <img src={colleaguePortrait(colleague)} alt="" />
    </span>
  );
}
