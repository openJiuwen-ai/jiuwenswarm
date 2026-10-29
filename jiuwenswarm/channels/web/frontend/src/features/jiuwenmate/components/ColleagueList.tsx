import { CirclePause, CirclePlay, FileUp, LayoutGrid, PencilLine, Plus, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import newColleagueIcon from '../../../assets/jiuwenmate/icons/new-colleague.svg';
import scheduleIcon from '../../../assets/jiuwenmate/icons/schedule.svg';
import type { JiuwenMateColleague } from '../types';
import { ActionMenu } from './ActionMenu';
import { ColleagueAvatar } from './ColleagueAvatar';

export interface ColleagueListHandlers {
  onCreateFromPlaza: () => void;
  onCreateCustom: () => void;
  onOpenSchedules: () => void;
  onManage: (id: string) => void;
  onToggleDisabled: (id: string) => void;
  onDelete: (id: string) => void;
}

interface ColleagueListProps extends ColleagueListHandlers {
  colleagues: readonly JiuwenMateColleague[];
  selectedId: string | null;
  schedulesSelected?: boolean;
  onSelect: (id: string) => void;
}

export function ColleagueList({
  colleagues,
  selectedId,
  schedulesSelected = false,
  onSelect,
  onCreateFromPlaza,
  onCreateCustom,
  onOpenSchedules,
  onManage,
  onToggleDisabled,
  onDelete,
}: ColleagueListProps) {
  const { t } = useTranslation();
  return (
    <aside className="jiuwenmate-colleague-list" data-testid="jiuwenmate-colleague-list">
      <header>
        <h2>{t('jiuwenmate.workspace.colleagues')}</h2>
      </header>
      <div className="jiuwenmate-colleague-list__actions">
        <ActionMenu
          label={t('jiuwenmate.workspace.newColleague')}
          align="start"
          className="jiuwenmate-colleague-list__new"
          testId="jiuwenmate-new-colleague-button"
          trigger={
            <>
              <img src={newColleagueIcon} alt="" aria-hidden />
              <span>{t('jiuwenmate.workspace.newColleague')}</span>
            </>
          }
          items={[
            { key: 'plaza', label: t('jiuwenmate.workspace.newFromPlaza'), icon: LayoutGrid, onSelect: onCreateFromPlaza },
            { key: 'custom', label: t('jiuwenmate.workspace.newCustom'), icon: Plus, onSelect: onCreateCustom },
            { key: 'local', label: t('jiuwenmate.workspace.newFromLocal'), icon: FileUp, disabled: true, onSelect: () => undefined },
          ]}
        />
        <button
          type="button"
          aria-pressed={schedulesSelected}
          onClick={onOpenSchedules}
          data-testid="jiuwenmate-open-schedules-button"
        >
          <img src={scheduleIcon} alt="" aria-hidden />
          <span>{t('jiuwenmate.workspace.tabs.schedules')}</span>
        </button>
      </div>
      <p className="jiuwenmate-colleague-list__label">{t('jiuwenmate.workspace.colleagueList')}</p>
      {colleagues.length === 0 ? (
        <p className="jiuwenmate-colleague-list__empty">{t('jiuwenmate.workspace.noColleagues')}</p>
      ) : (
        <div className="jiuwenmate-colleague-list__items">
          {colleagues.map((colleague) => (
            <div
              key={colleague.id}
              className="jiuwenmate-colleague-list__item"
              aria-current={!schedulesSelected && selectedId === colleague.id ? 'true' : undefined}
              data-testid="jiuwenmate-colleague-list-item"
              data-variant={colleague.id}
            >
              <button type="button" aria-pressed={!schedulesSelected && selectedId === colleague.id} onClick={() => onSelect(colleague.id)}>
                <ColleagueAvatar colleague={colleague} size="medium" />
                <strong>{colleague.name}</strong>
                {colleague.disabled ? <small>{t('jiuwenmate.office.disabledBadge')}</small> : null}
              </button>
              <ActionMenu
                label={t('jiuwenmate.workspace.more')}
                items={[
                  { key: 'manage', label: t('jiuwenmate.office.manage'), icon: PencilLine, onSelect: () => onManage(colleague.id) },
                  {
                    key: 'toggle',
                    label: colleague.disabled ? t('jiuwenmate.office.enable') : t('jiuwenmate.office.disable'),
                    icon: colleague.disabled ? CirclePlay : CirclePause,
                    onSelect: () => onToggleDisabled(colleague.id),
                  },
                  { key: 'delete', label: t('jiuwenmate.office.delete'), icon: Trash2, onSelect: () => onDelete(colleague.id) },
                ]}
              />
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
