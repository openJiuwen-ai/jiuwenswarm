import {
  ArrowUp,
  CalendarDays,
  ChartLine,
  ChartNoAxesColumn,
  ChevronDown,
  ChevronRight,
  CirclePause,
  CirclePlay,
  HardDrive,
  Monitor,
  Plus,
  Search,
  Trash2,
  UsersRound,
  type LucideIcon,
} from 'lucide-react';
import { useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ActionMenu } from '../components/ActionMenu';
import { ActivityHeatmap, HeatmapLegend } from '../components/ActivityHeatmap';
import { ColleagueAvatar } from '../components/ColleagueAvatar';
import { ColleagueCard } from '../components/ColleagueCard';
import { SegmentedTabs } from '../components/SegmentedTabs';
import { colleaguePortrait, customAvatarHue } from '../colleaguePortraits';
import { ScheduleBoard } from '../schedules/ScheduleBoard';
import type { Schedule, ScheduleDraft, ScheduleRun } from '../schedules/types';
import {
  activeChannelCount,
  activitySummary,
  createOfficeState,
  formatMonthDay,
  formatSummaryTokens,
  formatTableTokens,
  groupsInPeriod,
  hourlyActivity,
  hoverHeatmapCell,
  mergeDailyActivity,
  minutesSince,
  responseAge,
  periodHeatmap,
  selectOfficeMember,
  selectOfficeTab,
  setActivityColleague,
  setActivityPeriod,
  setActivityRange,
  setActivityScope,
  setActivityYear,
  usageChange,
  yearHeatmap,
  type JiuwenMateActivityRange,
  type JiuwenMateActivityScope,
  type JiuwenMateOfficeState,
  type JiuwenMateOfficeTab,
  type JiuwenMateWorkPeriod,
} from '../officeState';
import type { JiuwenMateActivity, JiuwenMateActivityMember, JiuwenMateColleague } from '../types';

export interface OfficeDashboardHandlers {
  onCreate: () => void;
  onChat: (id: string) => void;
  onManage: (id: string) => void;
  onToggleDisabled: (id: string) => void;
  onDelete: (id: string) => void;
  onOpenTrace: (colleagueId: string, channelId: string) => void;
  onCreateSchedule: (agentId: string, draft: ScheduleDraft) => void | Promise<void>;
  onUpdateSchedule: (planId: string, draft: ScheduleDraft) => void | Promise<void>;
  onSetScheduleEnabled: (planId: string, enabled: boolean) => void | Promise<void>;
  onDeleteSchedule: (planId: string) => void | Promise<void>;
  onLoadScheduleHistory: (planId: string) => Promise<{ plan: Schedule; runs: ScheduleRun[] }>;
}

interface OfficeDashboardPageProps extends OfficeDashboardHandlers {
  colleagues: readonly JiuwenMateColleague[];
  schedules: readonly Schedule[];
  savingSchedule: boolean;
  activity: JiuwenMateActivity | null;
  activityStatus: 'loading' | 'ready' | 'failed';
  activityError?: string;
  today?: Date;
}

type Setter = (update: (current: JiuwenMateOfficeState) => JiuwenMateOfficeState) => void;

const WORK_LOG_PERIODS = ['7d', '1m', '3m', 'all'] as const;
const PERIOD_AXIS_HOURS = [0, 3, 6, 9, 12, 15, 18, 21];

export function OfficeDashboardPage({
  colleagues,
  schedules,
  savingSchedule,
  activity,
  activityStatus,
  activityError = '',
  today = new Date(),
  ...handlers
}: OfficeDashboardPageProps) {
  const { t } = useTranslation();
  const [state, setState] = useState(() => createOfficeState(colleagues[0]?.id ?? null, today));
  const selected = colleagues.find((item) => item.id === state.selectedColleagueId) ?? colleagues[0];

  const tabs = [
    { value: 'members' as const, label: t('jiuwenmate.office.tabs.members') },
    { value: 'activity' as const, label: t('jiuwenmate.office.tabs.activity') },
    { value: 'tasks' as const, label: t('jiuwenmate.office.tabs.tasks') },
  ];

  return (
    <section className="jiuwenmate-page jiuwenmate-office" data-testid="jiuwenmate-office-dashboard">
      <h1 className="jiuwenmate-office__title">{t('jiuwenmate.office.title')}</h1>
      <div className="jiuwenmate-office__tabbar">
        <SegmentedTabs
          label={t('jiuwenmate.office.title')}
          tabs={tabs}
          value={state.tab}
          variant="underline"
          onChange={(tab: JiuwenMateOfficeTab) => setState((current) => selectOfficeTab(current, tab))}
        />
        <div className="jiuwenmate-office__tabbar-actions">
          {state.tab === 'members' ? (
            <button type="button" className="jiuwenmate-dark-button" onClick={handlers.onCreate} data-testid="jiuwenmate-office-create">
              {t('jiuwenmate.intro.create')}
            </button>
          ) : null}
          {state.tab === 'activity' ? (
            <>
              {state.activityScope === 'individual' && selected ? (
                <ColleagueSelect
                  colleagues={colleagues}
                  selected={selected}
                  onSelect={(id) => setState((current) => setActivityColleague(current, id))}
                />
              ) : null}
              <SegmentedTabs
                label={t('jiuwenmate.office.scope')}
                tabs={[
                  { value: 'all' as const, label: t('jiuwenmate.office.scopeAll') },
                  { value: 'individual' as const, label: t('jiuwenmate.office.scopeIndividual') },
                ]}
                value={state.activityScope}
                onChange={(scope: JiuwenMateActivityScope) => setState((current) => setActivityScope(current, scope))}
              />
            </>
          ) : null}
        </div>
      </div>
      {state.tab === 'members' ? <MembersTab colleagues={colleagues} handlers={handlers} /> : null}
      {state.tab === 'activity' && activityStatus !== 'ready' ? (
        <p className="jiuwenmate-office__notice" data-testid="jiuwenmate-activity-status">
          {activityStatus === 'loading' ? t('jiuwenmate.office.activityLoading') : t('jiuwenmate.office.activityFailed', { reason: activityError })}
        </p>
      ) : null}
      {state.tab === 'activity' && activityStatus === 'ready' && activity && state.activityScope === 'all' ? (
        <ActivityAllTab
          state={state}
          setState={setState}
          today={today}
          colleagues={colleagues}
          members={activity.members}
          onManage={handlers.onManage}
        />
      ) : null}
      {state.tab === 'activity' && activityStatus === 'ready' && activity && state.activityScope === 'individual' && selected ? (
        <ActivityIndividualTab
          colleagues={colleagues}
          selected={selected}
          member={activity.members.find((item) => item.agentId === selected.id) ?? null}
          today={today}
          onSelect={(id) => setState((current) => selectOfficeMember(current, id))}
          handlers={handlers}
        />
      ) : null}
      {state.tab === 'tasks' ? (
        <TasksTab
          schedules={schedules}
          colleagues={colleagues}
          activity={activity}
          saving={savingSchedule}
          handlers={handlers}
        />
      ) : null}
    </section>
  );
}

function memberMenu(
  t: (key: string) => string,
  colleague: JiuwenMateColleague,
  handlers: Pick<OfficeDashboardHandlers, 'onToggleDisabled' | 'onDelete'>,
) {
  return (
    <ActionMenu
      label={t('jiuwenmate.workspace.more')}
      items={[
        {
          key: 'toggle',
          label: colleague.disabled ? t('jiuwenmate.office.enable') : t('jiuwenmate.office.disable'),
          icon: colleague.disabled ? CirclePlay : CirclePause,
          onSelect: () => handlers.onToggleDisabled(colleague.id),
        },
        { key: 'delete', label: t('jiuwenmate.office.delete'), icon: Trash2, onSelect: () => handlers.onDelete(colleague.id) },
      ]}
    />
  );
}

function memberBadge(t: (key: string) => string, colleague: JiuwenMateColleague) {
  if (colleague.disabled) return { label: t('jiuwenmate.office.disabledBadge'), tone: 'muted' as const };
  if (!colleague.imConfigured) return { label: t('jiuwenmate.office.imMissing'), tone: 'warning' as const };
  return undefined;
}

function memberCard(
  t: (key: string) => string,
  colleague: JiuwenMateColleague,
  handlers: OfficeDashboardHandlers,
  options: { selected?: boolean; onSelect?: () => void } = {},
) {
  return (
    <ColleagueCard
      colleague={colleague}
      selected={options.selected}
      onSelect={options.onSelect}
      badge={memberBadge(t, colleague)}
      menu={memberMenu(t, colleague, handlers)}
      actions={[
        { label: t('jiuwenmate.office.chat'), onClick: (item) => handlers.onChat(item.id) },
        { label: t('jiuwenmate.office.manage'), primary: true, onClick: (item) => handlers.onManage(item.id) },
      ]}
    />
  );
}

function MembersTab({ colleagues, handlers }: { colleagues: readonly JiuwenMateColleague[]; handlers: OfficeDashboardHandlers }) {
  const { t } = useTranslation();
  return (
    <div className="jiuwenmate-office__members" data-testid="jiuwenmate-office-members">
      <button type="button" className="jiuwenmate-office__create" onClick={handlers.onCreate} data-testid="jiuwenmate-office-create-tile">
        <span>
          <Plus aria-hidden />
        </span>
        <strong>{t('jiuwenmate.office.createTile')}</strong>
      </button>
      {colleagues.map((colleague) => (
        <div key={colleague.id} className="jiuwenmate-office__member">
          {memberCard(t, colleague, handlers)}
        </div>
      ))}
    </div>
  );
}

function ColleagueSelect({
  colleagues,
  selected,
  onSelect,
}: {
  colleagues: readonly JiuwenMateColleague[];
  selected: JiuwenMateColleague;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <ActionMenu
      label={t('jiuwenmate.office.currentColleague')}
      className="jiuwenmate-select-menu jiuwenmate-select-menu--bordered"
      testId="jiuwenmate-office-colleague-select"
      trigger={
        <>
          <small>{t('jiuwenmate.office.currentColleague')}</small>
          <span>{selected.name}</span>
          <ChevronDown aria-hidden />
        </>
      }
      items={colleagues.map((colleague) => ({
        key: colleague.id,
        label: colleague.name,
        iconSrc: colleaguePortrait(colleague),
        iconHue: customAvatarHue(colleague) ?? undefined,
        onSelect: () => onSelect(colleague.id),
      }))}
    />
  );
}

function lastResponseText(t: (key: string, options?: Record<string, unknown>) => string, minutes: number | null): string {
  if (minutes === null) return t('jiuwenmate.office.noResponse');
  const age = responseAge(minutes);
  return t(`jiuwenmate.office.${age.unit}Ago`, { count: age.count });
}

function MetricBand({ items }: { items: { key: string; value: ReactNode; label: string; icon: LucideIcon; extra?: ReactNode }[] }) {
  return (
    <div className="jiuwenmate-metric-band" data-testid="jiuwenmate-metric-band">
      {items.map(({ key, value, label, icon: Icon, extra }) => (
        <div key={key} data-variant={key}>
          <strong>{value}</strong>
          <span>
            <Icon aria-hidden />
            {label}
          </span>
          {extra}
        </div>
      ))}
    </div>
  );
}

function SearchField({ value, onChange, placeholder }: { value: string; onChange: (value: string) => void; placeholder: string }) {
  return (
    <label className="jiuwenmate-search">
      <Search aria-hidden />
      <input value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} aria-label={placeholder} />
    </label>
  );
}

function ActivityAllTab({
  state,
  setState,
  today,
  colleagues,
  members,
  onManage,
}: {
  state: JiuwenMateOfficeState;
  setState: Setter;
  today: Date;
  colleagues: readonly JiuwenMateColleague[];
  members: readonly JiuwenMateActivityMember[];
  onManage: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [query, setQuery] = useState('');
  const daily = useMemo(() => mergeDailyActivity(members), [members]);
  const heatmap = useMemo(() => yearHeatmap(state.year, today, daily), [state.year, today, daily]);
  const periodRows = useMemo(
    () => periodHeatmap(state.periodStart, state.periodEnd, today, hourlyActivity(members.flatMap((item) => item.hours), state.periodStart, today)),
    [state.periodStart, state.periodEnd, today, members],
  );
  const summary = activitySummary(
    heatmap,
    colleagues.length,
    members.map((member) => ({ tokens: member.tokens, companionDays: member.companionDays, chats: member.replies, lastResponseMinutes: 0 })),
  );
  const keyword = query.trim();
  const ranked = colleagues
    .map((colleague) => {
      const member = members.find((item) => item.agentId === colleague.id);
      return {
        colleague,
        stats: {
          tokens: member?.tokens ?? 0,
          companionDays: member?.companionDays ?? 0,
          chats: member?.replies ?? 0,
          lastResponseMinutes: minutesSince(member?.lastResponseAt ?? null, today),
        },
      };
    })
    .filter((item) => !keyword || item.colleague.name.includes(keyword));
  const years = [today.getFullYear(), today.getFullYear() - 1];

  return (
    <div className="jiuwenmate-office__activity" data-testid="jiuwenmate-office-activity">
      <div className="jiuwenmate-office__section-head">
        <h2>{t('jiuwenmate.office.dataOverview')}</h2>
        <div className="jiuwenmate-office__filters">
          <SegmentedTabs
            label={t('jiuwenmate.office.range')}
            tabs={[
              { value: 'year' as const, label: t('jiuwenmate.office.rangeYear') },
              { value: 'period' as const, label: t('jiuwenmate.office.rangePeriod') },
            ]}
            value={state.activityRange}
            onChange={(range: JiuwenMateActivityRange) => setState((current) => setActivityRange(current, range))}
          />
          {state.activityRange === 'year' ? (
            <span className="jiuwenmate-office__year">
              <span>{t('jiuwenmate.office.year')}</span>
              <ActionMenu
                label={t('jiuwenmate.office.year')}
                className="jiuwenmate-select-menu jiuwenmate-select-menu--bordered"
                testId="jiuwenmate-office-year-select"
                trigger={
                  <>
                    <span>{state.year}</span>
                    <ChevronDown aria-hidden />
                  </>
                }
                items={years.map((year) => ({
                  key: String(year),
                  label: String(year),
                  onSelect: () => setState((current) => setActivityYear(current, year)),
                }))}
              />
            </span>
          ) : (
            <span className="jiuwenmate-office__period">
              <input
                type="date"
                value={state.periodStart}
                aria-label={t('jiuwenmate.office.periodStart')}
                onChange={(event) => setState((current) => setActivityPeriod(current, event.target.value, current.periodEnd))}
              />
              <span>{t('jiuwenmate.office.periodTo')}</span>
              <input
                type="date"
                value={state.periodEnd}
                aria-label={t('jiuwenmate.office.periodEnd')}
                onChange={(event) => setState((current) => setActivityPeriod(current, current.periodStart, event.target.value))}
              />
            </span>
          )}
        </div>
      </div>
      <MetricBand
        items={[
          { key: 'members', value: summary.members, label: t('jiuwenmate.office.metrics.members'), icon: UsersRound },
          { key: 'activeDays', value: summary.activeDays, label: t('jiuwenmate.office.metrics.activeDays'), icon: CalendarDays },
          { key: 'tasks', value: summary.tasks, label: t('jiuwenmate.office.metrics.tasks'), icon: Monitor },
          {
            key: 'tokens',
            value: (
              <>
                {formatSummaryTokens(summary.tokens)}
                <small>tokens</small>
              </>
            ),
            label: t('jiuwenmate.office.metrics.tokens'),
            icon: ChartNoAxesColumn,
          },
        ]}
      />
      <section className="jiuwenmate-office__card" data-testid="jiuwenmate-office-record">
        <header>
          <h3>
            {state.activityRange === 'year'
              ? t('jiuwenmate.office.yearRecord', { year: state.year })
              : t('jiuwenmate.office.periodRecord')}
          </h3>
          <HeatmapLegend />
        </header>
        {state.activityRange === 'year' ? (
          <ActivityHeatmap
            heatmap={heatmap}
            hoveredCellId={state.hoveredCellId}
            onHover={(cellId) => setState((current) => hoverHeatmapCell(current, cellId))}
          />
        ) : (
          <div className="jiuwenmate-period-grid" data-testid="jiuwenmate-period-heatmap">
            {periodRows.map((row) => (
              <div key={row.date} className="jiuwenmate-period-grid__row">
                <span className="jiuwenmate-period-grid__label">
                  <strong>{row.relative ? t(`jiuwenmate.office.${row.relative}`) : formatMonthDay(row.date)}</strong>
                  <small>{t('jiuwenmate.office.periodEntries', { count: row.entries, hours: row.hours })}</small>
                </span>
                {row.cells.map((cell) => (
                  <span key={cell.id} className="jiuwenmate-period-grid__cell" data-level={cell.level}>
                    {cell.value > 0 ? cell.value : ''}
                  </span>
                ))}
              </div>
            ))}
            <div className="jiuwenmate-period-grid__axis" aria-hidden>
              <span />
              {Array.from({ length: 24 }, (_, hour) => (
                <span key={hour}>{PERIOD_AXIS_HOURS.includes(hour) ? `${hour}:00` : ''}</span>
              ))}
            </div>
          </div>
        )}
      </section>
      <div className="jiuwenmate-office__section-head">
        <h2>{t('jiuwenmate.office.leaderboard', { count: ranked.length })}</h2>
        <SearchField value={query} onChange={setQuery} placeholder={t('jiuwenmate.common.searchPlaceholder')} />
      </div>
      <table className="jiuwenmate-table" data-testid="jiuwenmate-activity-table">
        <thead>
          <tr>
            {(['member', 'tokens', 'companionDays', 'chats', 'lastResponse', 'actions'] as const).map((column) => (
              <th key={column}>{t(`jiuwenmate.office.columns.${column}`)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ranked.length === 0 ? (
            <tr>
              <td colSpan={6} className="jiuwenmate-table__empty">
                {t('jiuwenmate.common.noData')}
              </td>
            </tr>
          ) : (
            ranked.map(({ colleague, stats }) => (
              <tr key={colleague.id}>
                <td>
                  <span className="jiuwenmate-table__member">
                    <ColleagueAvatar colleague={colleague} size="small" />
                    {colleague.name}
                  </span>
                </td>
                <td>{formatTableTokens(stats.tokens)}</td>
                <td>{stats.companionDays}</td>
                <td>{stats.chats}</td>
                <td>
                  {lastResponseText(t, stats.lastResponseMinutes)}
                </td>
                <td>
                  <button type="button" className="jiuwenmate-link-button jiuwenmate-link-button--blue" onClick={() => onManage(colleague.id)}>
                    {t('jiuwenmate.office.manage')}
                  </button>
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

function ActivityIndividualTab({
  colleagues,
  selected,
  member,
  today,
  onSelect,
  handlers,
}: {
  colleagues: readonly JiuwenMateColleague[];
  selected: JiuwenMateColleague;
  member: JiuwenMateActivityMember | null;
  today: Date;
  onSelect: (id: string) => void;
  handlers: OfficeDashboardHandlers;
}) {
  const { t } = useTranslation();
  const [period, setPeriod] = useState<JiuwenMateWorkPeriod>('7d');
  const [query, setQuery] = useState('');
  const carouselRef = useRef<HTMLDivElement>(null);
  const keyword = query.trim();
  const usage = usageChange(member?.days ?? [], period, today);
  const groups = (member?.groups ?? []).filter((group) => !keyword || group.name.includes(keyword));
  const percent = usage.percent === null ? t('jiuwenmate.office.noResponse') : `${usage.percent}%`;

  return (
    <div className="jiuwenmate-office__individual" data-testid="jiuwenmate-office-individual">
      <div className="jiuwenmate-office__carousel-frame">
        <div className="jiuwenmate-office__carousel" ref={carouselRef}>
          {colleagues.map((colleague) => (
            <div key={colleague.id} className="jiuwenmate-office__member">
              {memberCard(t, colleague, handlers, {
                selected: colleague.id === selected.id,
                onSelect: () => onSelect(colleague.id),
              })}
            </div>
          ))}
        </div>
        <button
          type="button"
          className="jiuwenmate-office__carousel-next"
          aria-label={t('jiuwenmate.office.next')}
          onClick={() => carouselRef.current?.scrollBy({ left: 320, behavior: 'smooth' })}
        >
          <ChevronRight aria-hidden />
        </button>
      </div>
      <div className="jiuwenmate-office__section-head">
        <h2>{t('jiuwenmate.office.workRecord')}</h2>
        <ActionMenu
          label={t('jiuwenmate.office.workRecord')}
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
      </div>
      <div className="jiuwenmate-metric-strip jiuwenmate-metric-strip--plain">
        {[
          {
            key: 'weeklyUsage',
            value: (
              <>
                {percent}
                {usage.percent !== null && usage.percent > 0 ? <ArrowUp aria-hidden className="jiuwenmate-trend-up" /> : null}
              </>
            ),
            icon: ChartLine,
          },
          {
            key: 'resources',
            value: (
              <>
                {formatSummaryTokens(usage.tokens)}
                <small>tokens</small>
              </>
            ),
            icon: HardDrive,
          },
          { key: 'joinedDays', value: member?.companionDays ?? 0, icon: CalendarDays },
          { key: 'channels', value: member ? activeChannelCount(member) : 0, icon: Monitor },
          { key: 'groupChats', value: groupsInPeriod(member?.groups ?? [], period, today), icon: UsersRound },
        ].map(({ key, value, icon: Icon }) => (
          <div key={key}>
            <strong>{value}</strong>
            <span>
              <Icon aria-hidden />
              {t(`jiuwenmate.profile.metrics.${key}`)}
            </span>
          </div>
        ))}
      </div>
      <div className="jiuwenmate-office__section-head">
        <h2>{t('jiuwenmate.office.groupChats', { count: groups.length })}</h2>
        <SearchField value={query} onChange={setQuery} placeholder={t('jiuwenmate.common.searchPlaceholder')} />
      </div>
      <table className="jiuwenmate-table" data-testid="jiuwenmate-channel-table">
        <thead>
          <tr>
            {(['group', 'channel', 'artifacts', 'tokens', 'lastResponse', 'actions'] as const).map((column) => (
              <th key={column}>{t(`jiuwenmate.office.channelColumns.${column}`)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => {
            const responded = minutesSince(group.lastResponseAt, today);
            return (
            <tr key={group.id}>
              <td>
                <span className="jiuwenmate-table__member">
                  <span className="jiuwenmate-letter-avatar" data-variant={group.id}>
                    {group.name.slice(0, 1)}
                  </span>
                  {group.name}
                </span>
              </td>
              <td>
                <em className="jiuwenmate-platform-badge">
                  {group.platform === 'feishu' ? t('jiuwenmate.im.feishu') : group.platform || t('jiuwenmate.office.noResponse')}
                </em>
              </td>
              <td>{group.artifacts}</td>
              <td>{formatTableTokens(group.tokens)}</td>
              <td>
                {lastResponseText(t, responded)}
              </td>
              <td>
                <button
                  type="button"
                  className="jiuwenmate-link-button jiuwenmate-link-button--blue"
                  onClick={() => handlers.onOpenTrace(selected.id, group.id)}
                  data-testid="jiuwenmate-open-trace"
                >
                  {t('jiuwenmate.workspace.viewTrace')}
                </button>
              </td>
            </tr>
            );
          })}
          {groups.length === 0 && (
            <tr>
              <td colSpan={6} className="jiuwenmate-table__empty">
                {t('jiuwenmate.common.noData')}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function TasksTab({
  schedules,
  colleagues,
  activity,
  saving,
  handlers,
}: {
  schedules: readonly Schedule[];
  colleagues: readonly JiuwenMateColleague[];
  activity: JiuwenMateActivity | null;
  saving: boolean;
  handlers: OfficeDashboardHandlers;
}) {
  return (
    <div className="jiuwenmate-office__tasks" data-testid="jiuwenmate-office-tasks">
      <ScheduleBoard
        schedules={schedules}
        colleagues={colleagues}
        activity={activity}
        saving={saving}
        onCreate={handlers.onCreateSchedule}
        onUpdate={handlers.onUpdateSchedule}
        onSetEnabled={handlers.onSetScheduleEnabled}
        onDelete={handlers.onDeleteSchedule}
        onLoadHistory={handlers.onLoadScheduleHistory}
      />
    </div>
  );
}
