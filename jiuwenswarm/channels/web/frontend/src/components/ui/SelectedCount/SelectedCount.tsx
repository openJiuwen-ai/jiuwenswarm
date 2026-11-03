import { useTranslation } from 'react-i18next';

export interface SelectedCountProps {
  count: number;
  testId?: string;
}

export function SelectedCount({ count, testId }: SelectedCountProps) {
  const { t } = useTranslation();
  return (
    <span className="text-[13px] text-text-muted" data-testid={testId}>
      {t('common.selectedCount', { count })}
    </span>
  );
}
