import { AlertTriangle, ChevronRight, Layers3 } from 'lucide-react';
import type { TFunction } from 'i18next';
import { memo, useEffect, useMemo, useState, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import type { WallClockDomain } from './diagnosis/diagnosisTimeSelection';
import { formatAxisClock } from './diagnosis/diagnosisTimeSelection';
import type { TrajectoryTimeRange } from './trajectory/timeline';
import type {
  ScheduledControlEvent,
  ScheduledReviewDecision,
  ScheduledTask,
  TeamControlModel,
} from './teamControlTimeline';
import css from './ScheduledControlTimeline.module.css';

export interface ScheduledControlTimelineProps {
  model: TeamControlModel;
  domain: WallClockDomain | null;
  viewport: TrajectoryTimeRange | null;
  mapTime?: (wallMs: number) => number;
  reviewRunCount: number;
  /** Resolve a member id to the label used by the member lanes. */
  memberDisplayName?: (memberName: string) => string;
  /** Yield vertical space while a member trace is expanded. */
  compact?: boolean;
  /** Changes whenever another row receives focus, so compact mode closes again. */
  compactKey?: string | null;
}

interface VisibleWindow {
  start: number;
  end: number;
}

function visibleWindow(
  domain: WallClockDomain | null,
  viewport: TrajectoryTimeRange | null,
): VisibleWindow | null {
  const start = viewport?.start ?? domain?.startMs;
  const end = viewport?.end ?? domain?.endMs;
  return start === undefined || end === undefined || end <= start ? null : { start, end };
}

function fractionAt(
  wallMs: number,
  window: VisibleWindow,
  mapTime?: (wallMs: number) => number,
): number {
  const mapped = mapTime === undefined ? wallMs : mapTime(wallMs);
  return Math.min(1, Math.max(0, (mapped - window.start) / (window.end - window.start)));
}

function taskInterval(task: ScheduledTask): { startMs: number; endMs: number } | null {
  const startMs = task.createdAtMs ?? task.events[0]?.timestampMs;
  const endMs = task.completedAtMs
    ?? task.updatedAtMs
    ?? task.events[task.events.length - 1]?.timestampMs
    ?? startMs;
  return startMs === undefined || endMs === undefined
    ? null
    : { startMs, endMs: Math.max(startMs, endMs) };
}

function overlaps(
  interval: { startMs: number; endMs: number },
  window: VisibleWindow,
  mapTime?: (wallMs: number) => number,
): boolean {
  const start = mapTime === undefined ? interval.startMs : mapTime(interval.startMs);
  const end = mapTime === undefined ? interval.endMs : mapTime(interval.endMs);
  return Math.max(start, end) >= window.start && Math.min(start, end) <= window.end;
}

function decisionText(decision: ScheduledReviewDecision | undefined, t: TFunction): string {
  if (decision?.kind === 'pass') return t('trajectory.team.schedule.decision.pass');
  if (decision?.kind === 'fail') return t('trajectory.team.schedule.decision.fail');
  if (decision?.kind === 'score') {
    return t('trajectory.team.schedule.decision.score', {
      value: Number.isInteger(decision.value) ? decision.value : decision.value.toFixed(2),
    });
  }
  return t('trajectory.team.schedule.decision.pending');
}

function eventText(event: ScheduledControlEvent, t: TFunction): string {
  if (event.kind === 'vote') {
    return `${t('trajectory.team.schedule.event.vote')} · ${decisionText(event.decision, t)}`;
  }
  return t(`trajectory.team.schedule.event.${event.kind}`);
}

function markerClass(event: ScheduledControlEvent): string {
  if (event.kind === 'revision') return css.markerRevision;
  if (event.kind !== 'vote') return css.markerEvent;
  if (event.decision?.kind === 'pass') return css.markerPass;
  if (event.decision?.kind === 'fail') return css.markerFail;
  if (event.decision?.kind === 'score') return css.markerScore;
  return css.markerPending;
}

function TaskDetail({
  task,
  onClose,
  memberDisplayName,
}: {
  task: ScheduledTask;
  onClose: () => void;
  memberDisplayName?: (memberName: string) => string;
}) {
  const { t } = useTranslation();
  const assignee = task.assignee === undefined
    ? undefined
    : (memberDisplayName?.(task.assignee) ?? task.assignee);
  const rounds = useMemo(() => {
    const grouped = new Map<number, ScheduledTask['reviews']>();
    for (const review of task.reviews) {
      grouped.set(review.round, [...(grouped.get(review.round) ?? []), review]);
    }
    return [...grouped.entries()].sort(([left], [right]) => left - right);
  }, [task.reviews]);
  const events = task.events.filter(event => (
    event.kind !== 'updated' && event.kind !== 'unblocked'
  ));

  return (
    <div className={css.detail} data-testid="team-trajectory-control-task-detail">
      <div className={css.detailHeader}>
        <div className={css.detailTitle}>
          <span className={css.phase} data-phase={task.phase}>
            {t(`trajectory.team.schedule.phase.${task.phase}`)}
          </span>
          <strong>{task.taskId}</strong>
        </div>
        <div className={css.detailMeta}>
          {assignee === undefined ? null : (
            <span>
              {t('trajectory.team.schedule.assignee')}: {assignee}
            </span>
          )}
          <button
            type="button"
            className={css.close}
            aria-label={t('trajectory.team.schedule.closeTask')}
            title={t('trajectory.team.schedule.closeTask')}
            onClick={onClose}
            data-testid="team-trajectory-control-task-close"
          >
            ×
          </button>
        </div>
      </div>

      <div className={css.rounds}>
        {rounds.length === 0 ? (
          <span className={css.muted}>{t('trajectory.team.schedule.noReviews')}</span>
        ) : rounds.map(([round, reviews]) => (
          <div
            className={css.round}
            key={round}
            data-testid="team-trajectory-control-round"
            data-variant={`${task.taskId}:r${round}`}
          >
            <span className={css.roundLabel}>R{round}</span>
            <div className={css.reviewList}>
              {reviews.map(review => (
                <span
                  className={css.review}
                  key={review.key}
                  data-decision={review.decision.kind}
                  data-testid="team-trajectory-control-review"
                  data-variant={`${task.taskId}:${round}:${review.reviewer}`}
                >
                  <span>{review.reviewer}</span>
                  <strong>{decisionText(review.decision, t)}</strong>
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className={css.events} data-testid="team-trajectory-control-event-list">
        {events.map(event => (
          <div
            className={css.eventRow}
            key={event.eventId}
            data-kind={event.kind}
            data-testid="team-trajectory-control-event"
            data-variant={`${event.kind}:${event.recordId}`}
          >
            <time>{formatAxisClock(event.timestampMs)}</time>
            <span>{eventText(event, t)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export const ScheduledControlTimeline = memo(function ScheduledControlTimeline({
  model,
  domain,
  viewport,
  mapTime,
  reviewRunCount,
  memberDisplayName,
  compact = false,
  compactKey = null,
}: ScheduledControlTimelineProps) {
  const { t } = useTranslation();
  const window = visibleWindow(domain, viewport);
  const [collapsed, setCollapsed] = useState(false);
  const [compactExpanded, setCompactExpanded] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const selectedTask = model.tasks.find(task => task.taskId === selectedTaskId);
  const effectivelyCollapsed = compact ? !compactExpanded : collapsed;

  useEffect(() => {
    setCompactExpanded(false);
  }, [compact, compactKey]);

  if (model.tasks.length === 0) return null;

  return (
    <section
      className={`${css.root} jiuwenTrajectoryTheme`}
      aria-label={t('trajectory.team.schedule.title')}
      data-compact={effectivelyCollapsed ? 'true' : 'false'}
      data-testid="team-trajectory-control-overview"
    >
      <header className={css.header}>
        <button
          type="button"
          className={css.toggle}
          aria-expanded={!effectivelyCollapsed}
          aria-label={t(effectivelyCollapsed
            ? 'trajectory.team.schedule.expand'
            : 'trajectory.team.schedule.collapse')}
          title={t(effectivelyCollapsed
            ? 'trajectory.team.schedule.expand'
            : 'trajectory.team.schedule.collapse')}
          onClick={() => {
            if (compact) setCompactExpanded(value => !value);
            else setCollapsed(value => !value);
          }}
          data-testid="team-trajectory-control-toggle"
        >
          <ChevronRight
            size={14}
            strokeWidth={2}
            className={effectivelyCollapsed ? '' : css.toggleExpanded}
            aria-hidden
          />
        </button>
        <span className={css.heading}>
          <Layers3 size={14} strokeWidth={2} aria-hidden />
          <strong>{t('trajectory.team.schedule.title')}</strong>
        </span>
        <span className={css.summary} data-testid="team-trajectory-control-summary">
          {t('trajectory.team.schedule.summary', {
            tasks: model.tasks.length,
            completed: model.completedTaskCount,
            reviews: reviewRunCount,
            reworks: model.reworkCount,
          })}
        </span>
        <span className={css.controlCount}>
          {t('trajectory.team.schedule.controlRecords', { count: model.controlRecordCount })}
        </span>
        {model.anomalies.length === 0 ? null : (
          <span className={css.anomalySummary} data-testid="team-trajectory-control-anomaly-summary">
            <AlertTriangle size={13} strokeWidth={2} aria-hidden />
            {t('trajectory.team.schedule.anomalies', { count: model.anomalies.length })}
          </span>
        )}
      </header>

      {effectivelyCollapsed || window === null ? null : (
        <div className={`${css.body} ${selectedTask === undefined ? '' : css.bodyWithDetail}`}>
          <div className={css.taskColumn}>
            <div className={css.taskList} role="list" data-testid="team-trajectory-control-task-list">
          {model.tasks.map((task, index) => {
            const interval = taskInterval(task);
            if (interval === null || !overlaps(interval, window, mapTime)) return null;
            const start = fractionAt(interval.startMs, window, mapTime);
            const end = fractionAt(interval.endMs, window, mapTime);
            const left = Math.min(start, end);
            const width = Math.max(0.006, Math.abs(end - start));
            const selected = task.taskId === selectedTaskId;
            const barStyle = {
              '--control-task-left': `${left * 100}%`,
              '--control-task-width': `${width * 100}%`,
            } as CSSProperties;

            return (
              <div key={task.taskId} role="listitem" className={css.taskListItem}>
                <button
                  type="button"
                  className={`${css.taskRow} ${selected ? css.taskRowSelected : ''}`}
                  onClick={() => {
                    if (compact) setCompactExpanded(true);
                    setSelectedTaskId(selected ? null : task.taskId);
                  }}
                  aria-expanded={selected}
                  title={task.taskId}
                  data-testid="team-trajectory-control-task"
                  data-variant={task.taskId}
                >
                <span className={css.taskLabel}>T{index + 1}</span>
                <span className={css.taskTrack}>
                  <span className={css.taskBar} data-phase={task.phase} style={barStyle} />
                  {task.events.map(event => (
                    <span
                      key={event.eventId}
                      className={`${css.marker} ${markerClass(event)}`}
                      style={{ left: `${fractionAt(event.timestampMs, window, mapTime) * 100}%` }}
                      title={`${formatAxisClock(event.timestampMs)} · ${eventText(event, t)}`}
                      aria-hidden
                    />
                  ))}
                  <ChevronRight size={12} strokeWidth={2} className={css.rowChevron} aria-hidden />
                  </span>
                </button>
              </div>
            );
          })}
            </div>

      {effectivelyCollapsed || model.anomalies.length === 0 ? null : (
        <div className={css.anomalyList}>
          {model.anomalies.map(anomaly => (
            <span
              className={css.anomaly}
              key={anomaly.eventId}
              title={anomaly.summary}
              data-testid="team-trajectory-control-anomaly"
              data-variant={anomaly.eventId}
            >
              <AlertTriangle size={12} strokeWidth={2} aria-hidden />
              <time>{formatAxisClock(anomaly.timestampMs)}</time>
              <span>{anomaly.memberName ?? t('trajectory.team.schedule.unknownMember')}</span>
              <span>{anomaly.summary ?? anomaly.detector ?? ''}</span>
            </span>
          ))}
        </div>
      )}
          </div>

      {effectivelyCollapsed || selectedTask === undefined ? null : (
        <TaskDetail
          task={selectedTask}
          onClose={() => setSelectedTaskId(null)}
          memberDisplayName={memberDisplayName}
        />
      )}
        </div>
      )}
    </section>
  );
});
