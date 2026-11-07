import { useTranslation } from 'react-i18next';
import unassignedAgentAvatar from '../../assets/未分配智能体头像.svg';

export function UnassignedTeamAvatar({ className }: { className?: string }) {
  const { t } = useTranslation();

  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full ${className ?? ''}`}
      role="img"
      aria-label={t('team.planning.unassignedAvatar')}
      data-testid="team-area-unassigned-avatar"
    >
      <img src={unassignedAgentAvatar} alt="" className="h-full w-full object-cover" />
    </span>
  );
}
