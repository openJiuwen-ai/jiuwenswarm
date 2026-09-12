import { AlertCircle, ChevronRight, CircleEllipsis, Lightbulb, ListTodo, Search, SquareTerminal, Wrench } from 'lucide-react';
import { memo, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ProcessingIcon from '../../assets/subagent/processing.svg?react';
import BackIcon from '../../assets/back.svg?react';
import { getSubagentStatusLabelKey } from '../../features/subagent/subagentStatusPresentation';
import { extractSubagentTasks, finalizeSubagentTasks, getSubagentActivityPreview, groupSubagentActivities, type SubagentActivityGroup } from '../../features/subagent/subagentActivityPresentation';
import { selectSubagentActivities, selectSubagentHistoryRestoring, selectSubagentResult, selectSubagentTurns, selectSubagents, useSubagentStore } from '../../stores/subagentStore';
import type { Subagent, SubagentActivity, SubagentActivityKind, SubagentTurn } from '../../types/subagent';
import { MemberTaskListBar, MemberTaskListItems, type MemberTaskListItem } from '../teamArea/MemberTaskList';
import { formatTime } from '../teamArea/shared';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { SubagentStatusIcon } from './SubagentStatusIcon';
import './Subagent.css';

function ActivityIcon({ activity }: { activity: SubagentActivity }) {
  const className = 'h-4 w-4 shrink-0';
  const toolName = activity.tool_name?.toLowerCase() ?? '';

  if (activity.kind === 'thinking') {
    return <Lightbulb className={className} aria-hidden="true" />;
  }
  if (activity.kind === 'error' || activity.ok === false) {
    return <AlertCircle className={`${className} text-danger`} aria-hidden="true" />;
  }
  if (activity.kind === 'truncated') {
    return <CircleEllipsis className={className} aria-hidden="true" />;
  }
  if (toolName.includes('search') || toolName.includes('fetch') || toolName.includes('web')) {
    return <Search className={className} aria-hidden="true" />;
  }
  if (toolName.includes('bash') || toolName.includes('terminal') || toolName.includes('shell')) {
    return <SquareTerminal className={className} aria-hidden="true" />;
  }
  if (toolName.includes('todo')) {
    return <ListTodo className={className} aria-hidden="true" />;
  }
  return <Wrench className={className} aria-hidden="true" />;
}

function activityLabel(kind: SubagentActivityKind, t: (key: string) => string): string {
  const labels: Record<SubagentActivityKind, string> = {
    thinking: t('subagent.activity.thinking'),
    tool_call: t('subagent.activity.toolCall'),
    tool_result: t('subagent.activity.toolResult'),
    error: t('subagent.activity.error'),
    truncated: t('subagent.activity.truncated'),
  };
  return labels[kind];
}

function activityToolLabel(toolName: string | null | undefined, t: (key: string) => string): string | null {
  const normalized = toolName?.trim().toLowerCase();
  if (!normalized) return null;
  if (normalized.includes('search') || normalized.includes('fetch') || normalized.includes('web')) {
    return t('subagent.activity.tools.search');
  }
  if (normalized.includes('bash') || normalized.includes('terminal') || normalized.includes('shell')) {
    return t('subagent.activity.tools.terminal');
  }
  if (normalized.includes('todo')) {
    return t('subagent.activity.tools.todo');
  }
  return toolName?.trim() || normalized;
}

type ActivityDetailRow = [label: string, value: string];

function buildActivityDetailRows(group: SubagentActivityGroup, t: (key: string) => string): ActivityDetailRow[] {
  const activity = group.activity;
  const latestActivity = group.activities[group.activities.length - 1] ?? activity;
  const rows: ActivityDetailRow[] = [
    [t('subagent.activity.fields.type'), activityLabel(activity.kind, t)],
    [t('subagent.activity.fields.time'), formatTime(latestActivity.at_ms) || '-'],
  ];

  if (activity.tool_name) {
    rows.push([t('subagent.activity.fields.tool'), activity.tool_name]);
  }

  if (activity.kind === 'tool_call') {
    rows.push([t('subagent.activity.fields.call'), activity.summary || '-']);
  } else if (activity.kind === 'tool_result') {
    rows.push([t('subagent.activity.fields.result'), activity.summary || '-']);
  } else {
    rows.push([t('subagent.activity.fields.content'), group.summary || '-']);
  }

  if (activity.ok !== undefined) {
    rows.push([
      t('subagent.activity.fields.status'),
      activity.ok ? t('subagent.activity.ok') : t('subagent.activity.error'),
    ]);
  }
  if (activity.dropped !== undefined) {
    rows.push([t('subagent.activity.fields.dropped'), String(activity.dropped)]);
  }

  return rows;
}

function ActivityRow({ group, isLast, isSubagentRunning }: { group: SubagentActivityGroup; isLast: boolean; isSubagentRunning: boolean }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const activity = group.activity;
  const latestActivity = group.activities[group.activities.length - 1] ?? activity;
  const isThinking = activity.kind === 'thinking';
  const isRunning = isLast && isSubagentRunning && (latestActivity.kind === 'thinking' || latestActivity.kind === 'tool_call');
  const label = isThinking
    ? t('subagent.activity.thinking')
    : activityToolLabel(activity.tool_name, t) ?? activityLabel(activity.kind, t);
  const summary = getSubagentActivityPreview(group);
  const timestamp = formatTime(latestActivity.at_ms);
  const detailRows = buildActivityDetailRows(group, t);
  const detailsId = `subagent-activity-details-${activity.activity_id}`;

  return (
    <li className="subagent-activity-row" data-testid="subagent-activity-row" data-variant={activity.activity_id}>
      <div className="subagent-activity-row__icon" aria-hidden="true">
        <ActivityIcon activity={activity} />
      </div>
      <div className="subagent-activity-row__body">
        <span className="subagent-activity-row__label" data-testid="subagent-activity-row-label">{label}</span>
        {summary && summary !== label ? <span className="subagent-activity-row__summary" data-testid="subagent-activity-row-summary"> | {summary}</span> : null}
        {activity.dropped ? <span className="subagent-activity-row__dropped" data-testid="subagent-activity-row-dropped"> +{activity.dropped}</span> : null}
      </div>
      {timestamp ? <span className="subagent-activity-row__timestamp" data-testid="subagent-activity-row-timestamp">{timestamp}</span> : null}
      <button
        type="button"
        className="subagent-activity-row__toggle"
        aria-expanded={expanded}
        aria-controls={detailsId}
        aria-label={t(expanded ? 'subagent.activity.collapse' : 'subagent.activity.expand')}
        onClick={() => setExpanded(value => !value)}
        data-testid="subagent-activity-row-toggle"
      >
        {isRunning ? (
          <ProcessingIcon className="subagent-activity-row__processing shrink-0 text-muted animate-spin" aria-label={t('subagent.running')} role="img" />
        ) : (
          <ChevronRight className={`subagent-activity-row__chevron ${expanded ? 'subagent-activity-row__chevron--expanded' : ''}`} aria-hidden="true" />
        )}
      </button>
      {expanded ? (
        <div id={detailsId} className="subagent-activity-row__detail" data-testid="subagent-activity-row-detail">
          <div className="subagent-activity-row__detail-rows">
            {detailRows.map(([detailLabel, detailValue]) => (
              <div key={detailLabel} className="subagent-activity-row__detail-row" data-testid="subagent-activity-row-detail-row" data-variant={detailLabel}>
                <span className="subagent-activity-row__detail-label">{detailLabel}</span>
                <span className="subagent-activity-row__detail-value">{detailValue}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </li>
  );
}

function SubagentDetail({ sessionId, subagentId, onBack }: { sessionId: string; subagentId: string; onBack?: () => void }) {
  const { t } = useTranslation();
  const [tasksExpanded, setTasksExpanded] = useState(false);
  const runtime = useSubagentStore(state => state.runtimes[sessionId]);
  const activities = selectSubagentActivities(runtime, subagentId);
  const historyRestoring = selectSubagentHistoryRestoring(runtime, subagentId);
  const result = selectSubagentResult(runtime, subagentId);
  const turns = selectSubagentTurns(runtime, subagentId);
  const subagent = runtime?.subagentsById[subagentId];

  if (!subagent) {
    return <div className="subagent-detail__state" role="status" data-testid="subagent-detail-empty">{t('subagent.empty')}</div>;
  }

  const taskDescription = subagent.task_description?.trim() || subagent.display_name;
  const hasFailed = subagent.closed_reason === 'failed' || subagent.turn_outcome === 'failed';
  const taskSectionId = `subagent-tasks-${subagentId}`;
  const visibleTurns: SubagentTurn[] = turns.length > 0
    ? turns
    : [{
      task_id: '__legacy__',
      task_description: taskDescription,
      started_at: subagent.created_at,
      ...(result ? { result } : {}),
    }];
  const hasTurnResult = visibleTurns.some(turn => Boolean(turn.result?.content?.trim()));
  const legacyFallbackResult = !historyRestoring && !hasTurnResult ? result : undefined;
  const latestTurnId = visibleTurns[visibleTurns.length - 1]?.task_id;
  const tasks = finalizeSubagentTasks(
    extractSubagentTasks(activities),
    subagent.turn_outcome === 'completed' && visibleTurns.some(turn => Boolean(turn.result?.content?.trim())),
    subagent.updated_at,
  );
  const taskListItems: MemberTaskListItem[] = tasks.map(task => ({
    id: task.id,
    title: task.content,
    detail: task.detail,
    status: task.status,
    raw: task.raw,
    statusHistory: task.statusHistory,
  }));

  return (
    <div className="subagent-detail" data-testid="subagent-detail">
      <div className="flex shrink-0 items-center gap-2 bg-card pl-4 pr-6 pt-6" data-testid="subagent-member-detail-section">
        {onBack ? (
          <button
            type="button"
            onClick={onBack}
            className="flex shrink-0 items-center text-sm text-text-muted hover:text-text"
            data-testid="subagent-member-detail-back"
          >
            <BackIcon className="text-text" />
          </button>
        ) : null}
        <div className="flex min-w-0 items-center gap-2">
          <h2 className="truncate text-sm font-semibold text-text" data-testid="subagent-detail-name">{subagent.display_name}</h2>
          {subagent.role ? <span className="truncate text-sm text-text-muted" data-testid="subagent-detail-role"> | {subagent.role}</span> : null}
          {subagent.status === 'closed' ? <span className="subagent-closed-badge" data-testid="subagent-detail-closed-badge">{t('subagent.closed')}</span> : null}
        </div>
        {subagent.status !== 'closed' || hasFailed || subagent.closed_reason === 'evicted' ? (
          <div className="ml-auto shrink-0">
            <SubagentStatusIcon status={subagent.status} closedReason={subagent.closed_reason} turnOutcome={subagent.turn_outcome} />
          </div>
        ) : null}
      </div>

      <div className="member-detail-body min-h-0 flex-1 overflow-y-auto px-12 pt-[26px] pb-7" data-testid="subagent-member-detail-body">
        {hasFailed && subagent.error ? (
          <div className="subagent-error-note" role="alert" data-testid="subagent-detail-error">
            {subagent.error.message}
          </div>
        ) : null}

        <div className="subagent-conversation" aria-live="polite" data-testid="subagent-detail-conversation">
          {visibleTurns.map(turn => {
            const turnActivities = turn.task_id === '__legacy__'
              ? activities
              : activities.filter(activity => activity.task_id === turn.task_id);
            const activityGroups = groupSubagentActivities(turnActivities);
            const turnResult = (historyRestoring && turn.result?.source === 'wait' ? undefined : turn.result)
              ?? (!historyRestoring && visibleTurns.length === 1 ? result : undefined)
              ?? (turn.task_id === latestTurnId ? legacyFallbackResult : undefined);
            const waitingForHistory = historyRestoring && turnResult == null;
            return (
              <section className="subagent-turn" key={turn.task_id} data-testid="subagent-turn" data-variant={turn.task_id}>
                {turn.task_description.trim() ? (
                  <div className="subagent-assignment" data-testid="subagent-turn-assignment">
                    <MarkdownRenderer content={turn.task_description} className="chat-text chat-markdown subagent-markdown" />
                  </div>
                ) : null}

                <div className="subagent-identity" data-testid="subagent-turn-identity">
                  <TeamMemberAvatar member={subagent.subagent_id} alt={subagent.display_name} className="h-8 w-8 rounded-xl" imageClassName="rounded-xl" />
                  <div className="subagent-message__name">{subagent.display_name}</div>
                </div>

                <div className="subagent-activity-section" data-testid="subagent-turn-activity-section">
                  {turnActivities.length > 0 ? (
                    <ol className="subagent-activity-list" aria-label={t('subagent.activityTitle')} data-testid="subagent-activity-list">
                      {activityGroups.map((group, index) => (
                        <ActivityRow
                          key={group.activity.activity_id}
                          group={group}
                          isLast={index === activityGroups.length - 1}
                          isSubagentRunning={subagent.status === 'running' && turn.task_id === latestTurnId}
                        />
                      ))}
                    </ol>
                  ) : null}
                </div>

                {waitingForHistory ? (
                  <div className="subagent-history-loading" role="status" aria-live="polite" data-testid="subagent-history-loading">
                    {t('subagent.historyLoading')}
                  </div>
                ) : null}

                {turnResult?.content?.trim() ? (
                  <div className="subagent-message" data-testid="subagent-turn-message">
                    <div className="subagent-message__body">
                      <MarkdownRenderer content={turnResult.content} className="chat-text chat-markdown subagent-markdown subagent-message__content" />
                    </div>
                  </div>
                ) : null}
              </section>
            );
          })}
          {activities.length === 0 && !visibleTurns.some(turn => turn.result?.content?.trim()) ? (
            <div className="subagent-detail__state" data-testid="subagent-detail-activity-empty">{t('subagent.activityEmpty')}</div>
          ) : null}
        </div>

      </div>

      <div className="subagent-detail__footer" data-testid="subagent-detail-footer">
        <MemberTaskListBar
          tasks={taskListItems}
          expanded={tasksExpanded}
          onToggle={() => setTasksExpanded(value => !value)}
        />
        {tasksExpanded ? (
          <div id={taskSectionId} className="max-h-[200px] overflow-y-auto px-5 pb-4">
            <MemberTaskListItems tasks={taskListItems} emptyLabel="subagent.tasksEmpty" />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function SubagentOverviewPanel({ sessionId, subagents, onSubagentClick }: { sessionId: string; subagents: Subagent[]; onSubagentClick: (subagentId: string) => void }) {
  const { t } = useTranslation();

  return (
    <section className="flex min-w-0 flex-1 flex-col bg-card" data-testid="team-area-member-overview" data-panel="subagents">
      <div className="min-h-0 flex-1 overflow-y-auto px-6 pt-6 pb-[48px] [scrollbar-gutter:stable]" data-testid="team-area-member-overview-body">
        {subagents.length === 0 ? (
          <div className="py-12 text-center text-sm text-text-muted" data-testid="team-area-member-overview-empty">
            {t('subagent.empty')}
          </div>
        ) : (
          <div className="flex flex-col gap-4" data-testid="team-area-member-overview-grid">
            {subagents.map((subagent, index) => (
              <SubagentOverviewCard
                key={subagent.subagent_id}
                sessionId={sessionId}
                subagent={subagent}
                sequence={index + 1}
                onClick={() => onSubagentClick(subagent.subagent_id)}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

const SubagentOverviewCard = memo(function SubagentOverviewCard({
  sessionId,
  subagent,
  sequence,
  onClick,
}: {
  sessionId: string;
  subagent: Subagent;
  sequence: number;
  onClick?: () => void;
}) {
  const { t } = useTranslation();
  const runtime = useSubagentStore(state => state.runtimes[sessionId]);
  const activities = selectSubagentActivities(runtime, subagent.subagent_id);
  const activityGroups = useMemo(() => groupSubagentActivities(activities), [activities]);
  const isRunning = subagent.status === 'running';

  return (
    <div
      data-testid="team-area-member-overview-card"
      data-variant={subagent.subagent_id}
      className="relative flex h-[240px] flex-col gap-3 overflow-hidden rounded-[8px] border-[1.5px] border-border bg-card p-4 text-left hover:border-[var(--color-action-primary)] transition-colors"
    >
      <button
        type="button"
        onClick={onClick}
        className="flex items-center gap-3 text-left cursor-pointer"
        data-testid="team-area-member-overview-card-header"
      >
        <span
          className="absolute left-0 top-0 flex h-[18px] w-[18px] items-center justify-center text-[12px] leading-[18px] text-text bg-[var(--color-member-card-badge-surface)] rounded-tl-[4px] rounded-br-[8px] rounded-tr-none rounded-bl-none"
          data-testid="team-area-member-overview-card-sequence"
        >
          {sequence}
        </span>
        <div className="relative shrink-0">
          <TeamMemberAvatar member={subagent.subagent_id} alt={subagent.display_name} className="h-8 w-8 rounded-full" imageClassName="rounded-full" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-normal text-text" data-testid="team-area-member-overview-card-name">
            {subagent.display_name}
          </div>
          <div className="mt-0.5 truncate text-xs text-text-muted" data-testid="team-area-member-overview-card-id">
            @{subagent.subagent_id}
          </div>
        </div>
        <SubagentStatusIcon status={subagent.status} closedReason={subagent.closed_reason} turnOutcome={subagent.turn_outcome} />
      </button>
      <div className="min-w-0 flex-1 overflow-hidden">
        <ol
          className="subagent-activity-list h-full overflow-y-auto"
          style={{ scrollbarGutter: 'stable' }}
          aria-label={t('subagent.activityTitle')}
          data-testid="team-area-member-overview-card-activities"
        >
          {activityGroups.length === 0 ? (
            <li className="flex h-full items-center justify-center text-sm text-text-muted">{t('subagent.activityEmpty')}</li>
          ) : (
            activityGroups.map((group, index) => (
              <ActivityRow
                key={group.activity.activity_id}
                group={group}
                isLast={index === activityGroups.length - 1}
                isSubagentRunning={isRunning && index === activityGroups.length - 1}
              />
            ))
          )}
        </ol>
      </div>
    </div>
  );
});

export function SubagentExpandedPanel({
  sessionId,
  selectedSubagentId,
  onSelectSubagent,
}: {
  sessionId: string;
  selectedSubagentId: string | null;
  onSelectSubagent: (subagentId: string | null) => void;
}) {
  const { t } = useTranslation();
  const runtime = useSubagentStore(state => state.runtimes[sessionId]);
  const subagents = selectSubagents(runtime);
  const activeCount = subagents.filter(subagent => subagent.status === 'running').length;
  const detailSubagent = selectedSubagentId ? subagents.find(subagent => subagent.subagent_id === selectedSubagentId) ?? null : null;

  const openDetail = (subagentId: string) => {
    onSelectSubagent(subagentId);
  };
  const backToOverview = () => {
    onSelectSubagent(null);
  };

  if (!runtime) {
    return <div className="subagent-detail__state" role="status" data-testid="subagent-expanded-empty">{t('subagent.empty')}</div>;
  }

  return (
    <div className="subagent-expanded-panel" data-testid="subagent-expanded-panel">
      <aside className="subagent-expanded-panel__list" aria-label={t('subagent.title')} data-testid="subagent-expanded-list">
        <div className="subagent-expanded-panel__list-heading px-[24px] pt-[24px]">
          <span className="text-sm text-text" data-testid="subagent-expanded-list-heading">{t('subagent.activeListTitle', { count: activeCount })}</span>
        </div>
        <div className="space-y-3 px-[24px] py-4">
          {subagents.map(subagent => {
            const statusLabel = t(getSubagentStatusLabelKey(subagent.status, subagent.closed_reason, subagent.turn_outcome));
            return (
              <button
                type="button"
                key={subagent.subagent_id}
                className={`subagent-expanded-row ${subagent.subagent_id === selectedSubagentId ? 'subagent-expanded-row--selected' : ''}`}
                onClick={() => openDetail(subagent.subagent_id)}
                aria-pressed={subagent.subagent_id === selectedSubagentId}
                aria-label={t('subagent.selectWithStatus', { name: subagent.display_name, status: statusLabel })}
                data-testid="subagent-expanded-row"
                data-variant={subagent.subagent_id}
              >
                <TeamMemberAvatar member={subagent.subagent_id} alt={subagent.display_name} className="h-8 w-8 rounded-xl" imageClassName="rounded-xl" />
                <span className="min-w-0 flex-1 text-left">
                  <span className="block truncate text-sm font-semibold text-text" data-testid="subagent-expanded-row-name">{subagent.display_name}</span>
                  {(subagent.role || subagent.task_description) ? (
                    <span className="block truncate text-xs text-text-muted" data-testid="subagent-expanded-row-role">{subagent.role || subagent.task_description}</span>
                  ) : null}
                </span>
                <SubagentStatusIcon status={subagent.status} closedReason={subagent.closed_reason} turnOutcome={subagent.turn_outcome} />
              </button>
            );
          })}
        </div>
      </aside>
      {detailSubagent ? (
        <section className="min-w-0 flex-1 overflow-hidden" data-testid="subagent-expanded-detail">
          <SubagentDetail key={detailSubagent.subagent_id} sessionId={sessionId} subagentId={detailSubagent.subagent_id} onBack={backToOverview} />
        </section>
      ) : (
        <SubagentOverviewPanel sessionId={sessionId} subagents={subagents} onSubagentClick={openDetail} />
      )}
    </div>
  );
}
