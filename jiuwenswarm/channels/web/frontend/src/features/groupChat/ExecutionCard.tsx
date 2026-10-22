import { CheckCircle2, Circle, CircleAlert, CirclePause, Loader2, Network, Users } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { GroupExecution, GroupMessage } from './client';
import { shortTaskName, taskCounts } from './state';

export function ExecutionCard({ execution, result }: { execution: GroupExecution; result?: GroupMessage }) {
  const { t } = useTranslation();
  const counts = taskCounts(execution.tasks ?? {});
  const title = shortTaskName(execution.title || execution.task, t('groupChat.taskTitle'), 10);
  return <article className="group-chat-execution" data-testid="group-chat-execution" data-variant={execution.execution_id}>
    <header className="group-chat-execution-heading">
      <Network size={16} aria-hidden /><strong data-testid="group-chat-execution-title">{title}</strong>
      <div className="group-chat-task-counts" data-testid="group-chat-task-counts">
        {(['total', 'running', 'completed', 'stopped', 'failed'] as const).map((key) => <span key={key}
          data-testid="group-chat-task-count" data-variant={key}>
          {t(`groupChat.counts.${key}`)} <b>{counts[key]}</b>
        </span>)}
      </div>
    </header>
    {Object.entries(execution.tasks ?? {}).map(([id, status]) => {
      const detail = execution.task_details?.[id];
      const running = ['running', 'in_progress', 'planning', 'in_review'].includes(status);
      const failed = ['failed', 'timed_out'].includes(status);
      const stopped = ['cancelled', 'stopped', 'interrupted'].includes(status);
      const Icon = status === 'completed' ? CheckCircle2 : running ? Loader2 : failed ? CircleAlert : stopped ? CirclePause : Circle;
      return <div className="group-chat-subtask" key={id} data-testid="group-chat-subtask" data-variant={id}>
        <div className="group-chat-subtask-info">
          <strong data-testid="group-chat-subtask-title">{shortTaskName(detail?.title, t('groupChat.unnamedTask'))}</strong>
          <small><Users size={13} aria-hidden />{detail?.member_id || execution.display_name || execution.member_name}</small>
        </div>
        <span className="group-chat-subtask-status" data-testid="group-chat-subtask-status" data-variant={status}>
          <Icon size={16} className={running ? 'group-chat-spinning' : undefined} aria-hidden />
          {t(`groupChat.status.${status}`, { defaultValue: status })}
        </span>
      </div>;
    })}
    {!counts.total && <div className="group-chat-no-subtasks" data-testid="group-chat-no-subtasks">
      <span>{t('groupChat.noSubtasks')}</span>
      <span data-testid="group-chat-execution-status" data-variant={execution.status}>
        {t(`groupChat.status.${execution.status}`, { defaultValue: execution.status })}
      </span>
    </div>}
    {result && <a className="group-chat-text-button" href={`#group-chat-message-${result.message_id}`}
      data-testid="group-chat-execution-result">{t('groupChat.viewResult')}</a>}
  </article>;
}
