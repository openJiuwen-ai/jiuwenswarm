import {
  CalendarDays,
  ChartLine,
  ChevronDown,
  CirclePause,
  CirclePlay,
  HardDrive,
  Monitor,
  PencilLine,
  Search,
  SquareArrowOutUpRight,
  Trash2,
  UsersRound,
  type LucideIcon,
} from 'lucide-react';
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import feishuIcon from '../../../assets/jiuwenmate/icons/feishu.svg';
import deepseekIcon from '../../../assets/jiuwenmate/icons/model-deepseek.svg';
import kimiIcon from '../../../assets/jiuwenmate/icons/model-kimi.png';
import scheduleEmpty from '../../../assets/jiuwenmate/illustrations/schedule-empty.png';
import { ActionMenu } from '../components/ActionMenu';
import { ActivityHeatmap, HeatmapLegend } from '../components/ActivityHeatmap';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import { JIUWENMATE_MODELS } from '../mockData';
import { halfYearHeatmap } from '../officeState';
import type { JiuwenMateColleague } from '../types';

export type JiuwenMateProfileSection =
  | 'overview'
  | 'persona'
  | 'model'
  | 'skills'
  | 'connectors'
  | 'plugins'
  | 'knowledge'
  | 'channels'
  | 'schedules';

export const PROFILE_SECTIONS: readonly JiuwenMateProfileSection[] = [
  'overview',
  'persona',
  'model',
  'skills',
  'connectors',
  'plugins',
  'knowledge',
  'channels',
  'schedules',
];

type WorkLogPeriod = '7d' | '1m' | '3m' | 'all';
const WORK_LOG_PERIODS: readonly WorkLogPeriod[] = ['7d', '1m', '3m', 'all'];
const MODEL_ICONS = { 'deepseek-v3.2': deepseekIcon, 'kimi-k2': kimiIcon } as const;
const DAY_MS = 24 * 60 * 60 * 1000;

interface ColleagueProfilePageProps {
  colleague: JiuwenMateColleague;
  initialSection?: JiuwenMateProfileSection;
  today?: Date;
  onOpenOffice: () => void;
  onChat: () => void;
  onConfigureIm: () => void;
  onToggleDisabled: () => void;
  onDelete: () => void;
}

export function formatJoinedAt(joinedAt: string): string {
  const date = new Date(joinedAt);
  if (Number.isNaN(date.getTime())) return joinedAt;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}/${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function joinedDays(joinedAt: string, today: Date): number {
  const joined = new Date(joinedAt);
  if (Number.isNaN(joined.getTime())) return 1;
  const startOfJoined = new Date(joined.getFullYear(), joined.getMonth(), joined.getDate());
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  return Math.max(1, Math.round((startOfToday.getTime() - startOfJoined.getTime()) / DAY_MS) + 1);
}

export function ColleagueProfilePage({
  colleague,
  initialSection = 'overview',
  today = new Date(),
  onOpenOffice,
  onChat,
  onConfigureIm,
  onToggleDisabled,
  onDelete,
}: ColleagueProfilePageProps) {
  const { t } = useTranslation();
  const [section, setSection] = useState<JiuwenMateProfileSection>(initialSection);

  return (
    <section className="jiuwenmate-page jiuwenmate-profile" data-testid="jiuwenmate-colleague-profile">
      <nav className="jiuwenmate-breadcrumb" aria-label={t('jiuwenmate.create.crumbLabel')}>
        <button type="button" onClick={onOpenOffice}>
          {t('jiuwenmate.nav.office')}
        </button>
        <span aria-hidden>/</span>
        <strong>{colleague.name}</strong>
      </nav>
      <div className="jiuwenmate-profile__frame">
        <nav className="jiuwenmate-profile__subnav" aria-label={t('jiuwenmate.profile.sectionsLabel')}>
          {PROFILE_SECTIONS.map((item) => (
            <button
              key={item}
              type="button"
              aria-current={section === item ? 'true' : undefined}
              onClick={() => setSection(item)}
              data-testid="jiuwenmate-profile-section"
              data-variant={item}
            >
              {t(`jiuwenmate.profile.sections.${item}`)}
            </button>
          ))}
        </nav>
        <div className="jiuwenmate-profile__content">
          <header className="jiuwenmate-profile__header">
            <ColleagueAvatar colleague={colleague} size="large" />
            <h1>{colleague.name}</h1>
            <button
              type="button"
              className="jiuwenmate-icon-button"
              aria-label={t('jiuwenmate.profile.edit')}
              onClick={() => setSection('persona')}
            >
              <PencilLine aria-hidden />
            </button>
            {colleague.disabled ? <em className="jiuwenmate-badge" data-tone="muted">{t('jiuwenmate.office.disabledBadge')}</em> : null}
            <span className="jiuwenmate-profile__header-actions">
              <ActionMenu
                label={t('jiuwenmate.workspace.more')}
                items={[
                  {
                    key: 'toggle',
                    label: colleague.disabled ? t('jiuwenmate.office.enable') : t('jiuwenmate.office.disable'),
                    icon: colleague.disabled ? CirclePlay : CirclePause,
                    onSelect: onToggleDisabled,
                  },
                  { key: 'delete', label: t('jiuwenmate.office.delete'), icon: Trash2, onSelect: onDelete },
                ]}
              />
              <button type="button" className="jiuwenmate-dark-button" onClick={onChat} data-testid="jiuwenmate-profile-chat">
                {t('jiuwenmate.office.chat')}
              </button>
            </span>
          </header>
          {section === 'overview' ? (
            <ProfileOverview colleague={colleague} today={today} onConfigureIm={onConfigureIm} />
          ) : (
            <ProfileSectionDetail colleague={colleague} section={section} onConfigureIm={onConfigureIm} />
          )}
        </div>
      </div>
    </section>
  );
}

function ProfileOverview({
  colleague,
  today,
  onConfigureIm,
}: {
  colleague: JiuwenMateColleague;
  today: Date;
  onConfigureIm: () => void;
}) {
  const { t } = useTranslation();
  const [period, setPeriod] = useState<WorkLogPeriod>('7d');
  const [half, setHalf] = useState<1 | 2>(today.getMonth() < 6 ? 1 : 2);
  const [hoveredCellId, setHoveredCellId] = useState<string | null>(null);
  const year = today.getFullYear();
  const heatmap = useMemo(() => halfYearHeatmap(year, half, []), [year, half]);

  const metrics: { key: string; value: number; icon: LucideIcon }[] = [
    { key: 'weeklyUsage', value: 0, icon: ChartLine },
    { key: 'resources', value: 0, icon: HardDrive },
    { key: 'joinedDays', value: joinedDays(colleague.joinedAt, today), icon: CalendarDays },
    { key: 'channels', value: colleague.imConfigured ? 1 : 0, icon: Monitor },
    { key: 'groupChats', value: 0, icon: UsersRound },
  ];

  return (
    <>
      <p className="jiuwenmate-profile__description">{colleague.description}</p>
      <p className="jiuwenmate-profile__joined">
        {t('jiuwenmate.profile.joinedAt', { time: formatJoinedAt(colleague.joinedAt) })}
      </p>
      {!colleague.imConfigured ? (
        <section className="jiuwenmate-profile__im" data-testid="jiuwenmate-profile-quick-im">
          <header>
            <strong>{t('jiuwenmate.profile.quickIm')}</strong>
            <button type="button" className="jiuwenmate-link-button" onClick={onConfigureIm}>
              <SquareArrowOutUpRight aria-hidden />
              {t('jiuwenmate.profile.goConfigure')}
            </button>
          </header>
          <div className="jiuwenmate-profile__im-row">
            <img src={feishuIcon} alt="" aria-hidden />
            <span>
              <strong>{t('jiuwenmate.im.feishu')}</strong>
              <small>{t('jiuwenmate.profile.feishuHint')}</small>
            </span>
            <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={onConfigureIm}>
              {t('jiuwenmate.profile.authorize')}
            </button>
          </div>
        </section>
      ) : null}

      <section className="jiuwenmate-profile__block">
        <header>
          <h2>{t('jiuwenmate.profile.workLog')}</h2>
          <ActionMenu
            label={t('jiuwenmate.profile.workLog')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{t(`jiuwenmate.profile.periods.${period}`)}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={WORK_LOG_PERIODS.map((item) => ({
              key: item,
              label: t(`jiuwenmate.profile.periods.${item}`),
              onSelect: () => setPeriod(item),
            }))}
          />
        </header>
        <div className="jiuwenmate-metric-strip" data-testid="jiuwenmate-profile-metrics">
          {metrics.map(({ key, value, icon: Icon }) => (
            <div key={key}>
              <strong>{value}</strong>
              <span>
                <Icon aria-hidden />
                {t(`jiuwenmate.profile.metrics.${key}`)}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section className="jiuwenmate-profile__block jiuwenmate-profile__block--card">
        <header>
          <h2>{t('jiuwenmate.profile.periodRecord')}</h2>
          <ActionMenu
            label={t('jiuwenmate.profile.periodRecord')}
            className="jiuwenmate-select-menu"
            trigger={
              <>
                <span>{t(half === 2 ? 'jiuwenmate.profile.secondHalf' : 'jiuwenmate.profile.firstHalf', { year })}</span>
                <ChevronDown aria-hidden />
              </>
            }
            items={[
              { key: 'second', label: t('jiuwenmate.profile.secondHalf', { year }), onSelect: () => setHalf(2) },
              { key: 'first', label: t('jiuwenmate.profile.firstHalf', { year }), onSelect: () => setHalf(1) },
            ]}
          />
        </header>
        <div className="jiuwenmate-profile__heat">
          <div>
            <ActivityHeatmap
              heatmap={heatmap}
              hoveredCellId={hoveredCellId}
              onHover={setHoveredCellId}
            />
            <HeatmapLegend />
          </div>
          <div className="jiuwenmate-task-ring">
            <span className="jiuwenmate-task-ring__circle">
              <strong>0</strong>
              <small>{t('jiuwenmate.profile.taskCount')}</small>
            </span>
            <dl>
              <dt>{t('jiuwenmate.profile.taskTypes')}</dt>
              {(['conversation', 'channel', 'schedule'] as const).map((kind) => (
                <dd key={kind} data-kind={kind}>
                  <i />
                  <span>{t(`jiuwenmate.profile.taskKinds.${kind}`)}</span>
                  <strong>0</strong>
                </dd>
              ))}
            </dl>
          </div>
        </div>
      </section>

      <ExecutionHistory />
    </>
  );
}

function ExecutionHistory() {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const columns = ['task', 'channel', 'status', 'tokens', 'lastResponse', 'actions'] as const;
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-history">
      <header>
        <h2>{t('jiuwenmate.profile.history')}</h2>
        <span className="jiuwenmate-profile__filters">
          <button type="button" className="jiuwenmate-select-button">
            {t('jiuwenmate.profile.allChannels')}
            <ChevronDown aria-hidden />
          </button>
          <button type="button" className="jiuwenmate-select-button">
            {t('jiuwenmate.profile.allStatus')}
            <ChevronDown aria-hidden />
          </button>
          <label className="jiuwenmate-search">
            <Search aria-hidden />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('jiuwenmate.common.searchPlaceholder')}
              aria-label={t('jiuwenmate.common.searchPlaceholder')}
            />
          </label>
        </span>
      </header>
      <table className="jiuwenmate-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{t(`jiuwenmate.profile.historyColumns.${column}`)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            <td colSpan={columns.length} className="jiuwenmate-table__empty">
              {t('jiuwenmate.profile.historyEmpty')}
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  );
}

function ProfileSectionDetail({
  colleague,
  section,
  onConfigureIm,
}: {
  colleague: JiuwenMateColleague;
  section: Exclude<JiuwenMateProfileSection, 'overview'>;
  onConfigureIm: () => void;
}) {
  const { t } = useTranslation();
  const title = t(`jiuwenmate.profile.sections.${section}`);

  if (section === 'persona') {
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <dl className="jiuwenmate-profile__facts">
          <dt>{t('jiuwenmate.create.name')}</dt>
          <dd>{colleague.name}</dd>
          <dt>{t('jiuwenmate.create.description')}</dt>
          <dd>{colleague.description}</dd>
          <dt>{t('jiuwenmate.create.role')}</dt>
          <dd>{colleague.tags.length > 0 ? colleague.tags.join('、') : '-'}</dd>
        </dl>
      </section>
    );
  }

  if (section === 'model') {
    const model = JIUWENMATE_MODELS.find((item) => item.id === colleague.capabilities.model) ?? JIUWENMATE_MODELS[0];
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-model-card">
          <img src={MODEL_ICONS[model.id]} alt="" aria-hidden />
          <strong>{model.label}</strong>
        </div>
      </section>
    );
  }

  if (section === 'channels') {
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-profile__im-row jiuwenmate-profile__im-row--bordered">
          <img src={feishuIcon} alt="" aria-hidden />
          <span>
            <strong>{t('jiuwenmate.im.feishu')}</strong>
            <small>{t('jiuwenmate.profile.feishuHint')}</small>
          </span>
          {colleague.imConfigured ? (
            <em className="jiuwenmate-badge" data-tone="success">{t('jiuwenmate.profile.authorized')}</em>
          ) : (
            <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={onConfigureIm}>
              {t('jiuwenmate.profile.authorize')}
            </button>
          )}
        </div>
      </section>
    );
  }

  if (section === 'schedules') {
    return (
      <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
        <h2>{title}</h2>
        <div className="jiuwenmate-empty-state">
          <img src={scheduleEmpty} alt="" aria-hidden />
          <p>{t('jiuwenmate.workspace.noSchedules')}</p>
        </div>
      </section>
    );
  }

  const items = colleague.capabilities[section];
  return (
    <section className="jiuwenmate-profile__block" data-testid="jiuwenmate-profile-detail" data-variant={section}>
      <h2>{title}</h2>
      {items.length === 0 ? (
        <p className="jiuwenmate-capability-empty">{t(`jiuwenmate.create.empty.${section}`)}</p>
      ) : (
        <div className="jiuwenmate-capability-grid">
          {items.map((item) => (
            <span key={item} className="jiuwenmate-capability-chip">
              {item}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
