import type { ReactNode } from 'react';
import { CheckCircle2, Circle, CircleAlert, CirclePause, Network, Users } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { GroupExecution, GroupMessage } from './client';
import { shortTaskName, taskCounts } from './state';

const RUNNING = ['running', 'in_progress', 'planning', 'in_review'];
const FAILED = ['failed', 'timed_out'];
const STOPPED = ['cancelled', 'stopped', 'interrupted'];

type RenderAvatar = (member: string) => ReactNode;

/** Circular progress ring; without a known percentage it spins as an indeterminate indicator. */
function TaskStatus({ status, percent, testId, labelOverride }: {
  status: string; percent?: number; testId: string; labelOverride?: string;
}) {
  const { t } = useTranslation();
  const label = labelOverride ?? t(`groupChat.status.${status}`, { defaultValue: status });
  const known = typeof percent === 'number' && Number.isFinite(percent);
  if (RUNNING.includes(status) && known) {
    const value = Math.min(100, Math.max(0, Math.round(percent)));
    const radius = 18;
    const circumference = 2 * Math.PI * radius;
    return <span className="group-chat-subtask-status is-running" data-testid={testId} data-variant={status}
      title={label} aria-label={label} role="status">
      <svg viewBox="0 0 44 44" className="group-chat-ring" aria-hidden>
        <circle className="group-chat-ring-track" cx="22" cy="22" r={radius} />
        <circle className="group-chat-ring-value" cx="22" cy="22" r={radius}
          strokeDasharray={circumference} strokeDashoffset={circumference * (1 - value / 100)} />
      </svg>
      <b data-testid="group-chat-progress-percent">{value}<small>%</small></b>
    </span>;
  }
  if (RUNNING.includes(status)) {
    // Streaming events carry no progress, so show an icon-sized spinner next to the label.
    return <span className="group-chat-subtask-status" data-testid={testId} data-variant={status} role="status">
      <svg viewBox="0 0 16 16" width={16} height={16} className="group-chat-mini-ring group-chat-spinning" aria-hidden>
        <circle className="group-chat-ring-track" cx="8" cy="8" r="6.5" />
        <circle className="group-chat-ring-value" cx="8" cy="8" r="6.5" strokeDasharray="10.2 40.8" />
      </svg>
      {label}
    </span>;
  }
  const Icon = status === 'completed' ? CheckCircle2 : FAILED.includes(status) ? CircleAlert
    : STOPPED.includes(status) ? CirclePause : Circle;
  return <span className="group-chat-subtask-status" data-testid={testId} data-variant={status} role="status">
    <Icon size={16} aria-hidden />{label}
  </span>;
}

function Assignee({ member, name, renderAvatar, testId }: {
  member: string; name: string; renderAvatar?: RenderAvatar; testId?: string;
}) {
  return <small data-testid={testId}>
    {renderAvatar ? renderAvatar(member) : <Users size={13} aria-hidden />}{name}
  </small>;
}

export function ExecutionCard({ execution, result, renderAvatar }: {
  execution: GroupExecution; result?: GroupMessage; renderAvatar?: RenderAvatar;
}) {
  const { t } = useTranslation();
  const singleAgent = execution.execution_kind === 'agent';
  const counts = taskCounts(execution.tasks ?? {});
  const title = shortTaskName(execution.title || execution.task, t('groupChat.taskTitle'), 10);
  const owner = execution.member_name || execution.owner_member || execution.display_name;
  const ownerName = execution.display_name || execution.member_name || '';
  const progress = execution.progress && execution.progress.total > 0
    ? (execution.progress.completed / execution.progress.total) * 100 : undefined;
  return <article className="group-chat-execution" data-testid="group-chat-execution" data-variant={execution.execution_id}>
    <header className="group-chat-execution-heading">
      <Network size={16} aria-hidden /><strong data-testid="group-chat-execution-title"
        title={shortTaskName(execution.title || execution.task, t('groupChat.taskTitle'), Infinity)}>{title}</strong>
      {!singleAgent && <div className="group-chat-task-counts" data-testid="group-chat-task-counts">
        {(['total', 'running', 'completed', 'stopped', 'failed'] as const).map((key) => <span key={key}
          data-testid="group-chat-task-count" data-variant={key}>
          {t(`groupChat.counts.${key}`)} <b>{counts[key]}</b>
        </span>)}
      </div>}
    </header>
    <div className="group-chat-subtasks" data-testid="group-chat-subtasks">
      {!singleAgent && Object.entries(execution.tasks ?? {}).map(([id, status]) => {
        const detail = execution.task_details?.[id];
        const member = detail?.member_id || owner;
        const teammate = detail?.member_id && detail.member_id !== owner ? detail.member_id : '';
        const assigneeName = teammate ? `${ownerName}｜${teammate}` : ownerName;
        return <div className="group-chat-subtask" key={id} data-testid="group-chat-subtask" data-variant={id}>
          <div className="group-chat-subtask-info">
            <strong data-testid="group-chat-subtask-title" title={detail?.title}>
              {shortTaskName(detail?.title, t('groupChat.unnamedTask'))}
            </strong>
            <Assignee member={member} name={assigneeName} renderAvatar={renderAvatar}
              testId="group-chat-subtask-assignee" />
          </div>
          <TaskStatus status={status} testId="group-chat-subtask-status" />
        </div>;
      })}
      {singleAgent && <div className="group-chat-subtask" data-testid="group-chat-execution-summary">
        <div className="group-chat-subtask-info">
          <strong data-testid="group-chat-execution-task" title={execution.title || execution.task}>
            {shortTaskName(execution.title || execution.task, t('groupChat.taskTitle'))}
          </strong>
          <Assignee member={owner} name={ownerName} renderAvatar={renderAvatar}
            testId="group-chat-execution-member" />
        </div>
        <TaskStatus status={execution.status} percent={progress} testId="group-chat-execution-status" />
      </div>}
    </div>
    {!singleAgent && !counts.total && <div className="group-chat-no-subtasks" data-testid="group-chat-no-subtasks">
      <span>{t('groupChat.noSubtasks')}</span>
      <TaskStatus status={execution.status} percent={progress} testId="group-chat-execution-status"
        labelOverride={RUNNING.includes(execution.status) ? t('groupChat.generatingTasks') : undefined} />
    </div>}
    {result && <a className="group-chat-text-button" href={`#group-chat-message-${result.message_id}`}
      data-testid="group-chat-execution-result">{t('groupChat.viewResult')}</a>}
  </article>;
}
