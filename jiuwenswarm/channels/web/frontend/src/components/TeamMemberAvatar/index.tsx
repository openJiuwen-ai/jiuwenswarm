import clsx from 'clsx';
import { resolveTeamMemberAvatar } from '../../utils/teamMemberAvatar';

interface TeamMemberAvatarProps {
  member?: string;
  className?: string;
  imageClassName?: string;
  alt?: string;
}

export function TeamMemberAvatar({
  member,
  className,
  imageClassName,
  alt,
}: TeamMemberAvatarProps): JSX.Element {
  const avatar = resolveTeamMemberAvatar(member);
  const defaultImageRadius = avatar.kind === 'user' ? 'rounded-xl' : 'rounded-2xl';

  return (
    <div
      className={clsx(
        className ? null : 'h-8 w-8',
        'shrink-0 overflow-hidden rounded-xl bg-transparent',
        className
      )}
      style={avatar.backgroundColor ? { backgroundColor: avatar.backgroundColor } : undefined}
    >
      <img
        src={avatar.src}
        alt={alt ?? `${member || 'Unknown'} avatar`}
        className={clsx('h-full w-full object-cover', defaultImageRadius, imageClassName)}
      />
    </div>
  );
}
