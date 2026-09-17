import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';

export type ProjectCreateMode = 'blank' | 'existing';

interface ProjectCreateMenuProps {
  onCreate: (mode: ProjectCreateMode) => void;
  itemClassName: string;
  blankIcon: ReactNode;
  existingIcon: ReactNode;
}

export function ProjectCreateMenu({
  onCreate,
  itemClassName,
  blankIcon,
  existingIcon,
}: ProjectCreateMenuProps) {
  const { t } = useTranslation();
  return (
    <>
      <button
        type="button"
        className={itemClassName}
        onClick={() => onCreate('blank')}
        role="menuitem"
        data-testid="multi-session-project-create-menu-blank"
      >
        {blankIcon}
        <span>{t('multiSession.project.createBlank')}</span>
      </button>
      <button
        type="button"
        className={itemClassName}
        onClick={() => onCreate('existing')}
        role="menuitem"
        data-testid="multi-session-project-create-menu-existing"
      >
        {existingIcon}
        <span>{t('multiSession.project.selectExisting')}</span>
      </button>
    </>
  );
}
