import type { SkillDevSessionSummary } from '../../types/skilldev';

interface SkillDevSessionsTabProps {
  sessions: SkillDevSessionSummary[];
  loading: boolean;
  restoringTaskId: string | null;
  activeTaskId: string | null;
  onRefresh: () => void;
  onRestore: (taskId: string) => void;
}

export function SkillDevSessionsTab(props: SkillDevSessionsTabProps) {
  const {
    sessions,
    loading,
    restoringTaskId,
    activeTaskId,
    onRefresh,
    onRestore,
  } = props;

  return (
    <div className="h-full flex flex-col bg-bg">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between">
        <div className="text-sm font-medium text-text">会话列表</div>
        <button
          onClick={onRefresh}
          className="px-2.5 py-1.5 text-xs rounded border border-border text-text-muted hover:text-text hover:bg-hover"
        >
          刷新
        </button>
      </div>

      <div className="flex-1 overflow-auto">
        {loading ? (
          <div className="p-4 text-sm text-text-muted">加载中...</div>
        ) : sessions.length === 0 ? (
          <div className="p-4 text-sm text-text-muted">暂无可恢复会话</div>
        ) : (
          <div className="p-3 space-y-2">
            {sessions.map((session) => {
              const isActive = activeTaskId === session.task_id;
              const isRestoring = restoringTaskId === session.task_id;
              return (
                <div
                  key={session.task_id}
                  className={`rounded-lg border p-3 ${
                    isActive ? 'border-accent bg-accent-subtle' : 'border-border bg-secondary'
                  }`}
                >
                  <div className="text-xs text-text-muted break-all">{session.task_id}</div>
                  {session.title ? (
                    <div className="mt-1 text-sm text-text truncate" title={session.title}>
                      {session.title}
                    </div>
                  ) : null}
                  <div className="mt-1 text-sm text-text">
                    {session.runner === 'agent'
                      ? `状态：${session.status_label ?? session.status ?? session.stage}`
                      : `阶段：${session.stage}`}
                    {session.todo_progress ? (
                      <span className="text-text-muted"> · {session.todo_progress}</span>
                    ) : null}
                  </div>
                  <div className="mt-1 text-xs text-text-muted">更新时间：{session.updated_at || '-'}</div>
                  <div className="mt-2 flex items-center justify-between">
                    <span
                      className={`text-xs ${
                        session.is_suspended ? 'text-warning' : 'text-text-muted'
                      }`}
                    >
                      {session.is_suspended ? '已挂起' : '进行中/已完成'}
                    </span>
                    <button
                      onClick={() => onRestore(session.task_id)}
                      disabled={isRestoring}
                      className="px-2.5 py-1.5 text-xs rounded border border-border text-text hover:bg-hover disabled:opacity-60"
                    >
                      {isRestoring ? '恢复中...' : '恢复'}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
