import { useState, type ReactNode } from 'react';
import { ArrowUpToLine, GripVertical, MoreHorizontal, X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { controlApplicationTask, useApplicationTaskStore, type ApplicationTaskAction } from './taskProgressStore';

const DRAG_TYPE = 'application/x-jiuwen-task';

/** Controls plugin jobs in the existing progress row; ordinary todos stay unchanged. */
export function ApplicationTaskControls({ taskId, children }: { taskId: string; children: ReactNode }) {
  const { t } = useTranslation();
  const sessions = useApplicationTaskStore((state) => state.sessions);
  const tasks = Object.values(sessions).flat();
  const task = tasks.find((item) => `application:${item.pluginId}:${item.id}` === taskId);
  const [busy, setBusy] = useState(false);
  const [menu, setMenu] = useState(false);
  const [error, setError] = useState('');
  if (!task?.searchSessionId) return <>{children}</>;
  const pending = tasks
    .filter(
      (item) =>
        item.searchSessionId === task.searchSessionId && item.status === 'queued' && (item.queuePosition ?? 0) > 0,
    )
    .sort((a, b) => (a.queuePosition ?? 0) - (b.queuePosition ?? 0));
  const index = pending.findIndex((item) => item.id === task.id);
  const movable = index >= 0 && !busy;
  const stoppable = task.status === 'queued' || task.status === 'running';
  const label = (key: string) => t(`chat.applicationTasks.controls.${key}`);
  const run = async (action: ApplicationTaskAction, beforeId?: string, target = task) => {
    setBusy(true);
    setError('');
    setMenu(false);
    try {
      await controlApplicationTask(target, action, beforeId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : label('failed'));
    } finally {
      setBusy(false);
    }
  };
  const buttonClass = 'rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text disabled:opacity-40';
  return (
    <div
      className="relative flex min-w-0 flex-1 items-center gap-1"
      onDragOver={(event) => {
        if (movable && event.dataTransfer.types.includes(DRAG_TYPE)) event.preventDefault();
      }}
      onDrop={(event) => {
        event.preventDefault();
        event.stopPropagation();
        const source = pending.find((item) => item.id === event.dataTransfer.getData(DRAG_TYPE));
        if (source && source.id !== task.id && movable) void run('before', task.id, source);
      }}
    >
      {movable && (
        <span
          draggable
          tabIndex={0}
          title={label('drag')}
          aria-label={label('drag')}
          className="cursor-grab text-text-muted"
          onDragStart={(event) => {
            event.dataTransfer.setData(DRAG_TYPE, task.id);
            event.dataTransfer.effectAllowed = 'move';
          }}
        >
          <GripVertical size={14} />
        </span>
      )}
      {children}
      {movable && index > 0 && (
        <button
          type="button"
          className={buttonClass}
          title={label('next')}
          aria-label={label('next')}
          onClick={(event) => {
            event.stopPropagation();
            void run('next');
          }}
        >
          <ArrowUpToLine size={14} />
        </button>
      )}
      {stoppable && (
        <button
          type="button"
          className={buttonClass}
          disabled={busy}
          title={label('stop')}
          aria-label={label('stop')}
          onClick={(event) => {
            event.stopPropagation();
            void run('cancel');
          }}
        >
          <X size={14} />
        </button>
      )}
      {movable && (
        <button
          type="button"
          className={buttonClass}
          title={label('more')}
          aria-label={label('more')}
          aria-expanded={menu}
          onClick={(event) => {
            event.stopPropagation();
            setMenu(!menu);
          }}
        >
          <MoreHorizontal size={14} />
        </button>
      )}
      {menu && (
        <div
          className="absolute right-0 top-full z-50 flex min-w-max flex-col rounded border border-border bg-panel p-1 shadow-lg"
          onClick={(event) => event.stopPropagation()}
        >
          <button
            type="button"
            className={buttonClass}
            disabled={index <= 0}
            onClick={() => void run('before', pending[index - 1]?.id)}
          >
            {label('up')}
          </button>
          <button
            type="button"
            className={buttonClass}
            disabled={index < 0 || index === pending.length - 1}
            onClick={() => void run('before', pending[index + 2]?.id || '')}
          >
            {label('down')}
          </button>
          <button type="button" className={buttonClass} onClick={() => void run('preempt')}>
            {label('preempt')}
          </button>
          <button type="button" className={buttonClass} onClick={() => setMenu(false)}>
            {label('close')}
          </button>
        </div>
      )}
      {error && (
        <div
          role="alert"
          className="absolute right-0 top-full z-50 max-w-xs rounded border border-border bg-panel p-2 text-xs text-warn"
          onClick={(event) => event.stopPropagation()}
        >
          {error}
          <button type="button" className={buttonClass} onClick={() => setError('')}>
            {label('close')}
          </button>
        </div>
      )}
    </div>
  );
}
