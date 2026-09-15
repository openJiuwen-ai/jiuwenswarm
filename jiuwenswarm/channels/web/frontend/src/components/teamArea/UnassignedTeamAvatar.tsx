import { useTranslation } from 'react-i18next';

export function UnassignedTeamAvatar({ className }: { className?: string }) {
  const { t } = useTranslation();

  return (
    <span
      className={`inline-flex shrink-0 items-center justify-center overflow-hidden rounded-full border border-border bg-card text-[12px] font-medium text-muted ${className ?? ''}`}
      role="img"
      aria-label={t('team.planning.unassignedAvatar')}
      title={t('team.planning.unassigned')}
    >
      --
    </span>
  );
}
