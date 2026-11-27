/**
 * TodoList 组件
 *
 * 显示任务列表，支持状态图标和实时更新
 */

import { useTranslation } from 'react-i18next';
import { useTodoStore } from '../../stores';
import { TodoItem } from './TodoItem';

export function TodoList() {
  const { t } = useTranslation();
  const { todos } = useTodoStore();

  if (todos.length === 0) {
    return (
      <div className="p-4 h-full flex flex-col items-center justify-center text-center">
        <svg className="w-10 h-10 text-text-muted opacity-30 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <p className="text-text-muted text-sm">{t('todoList.empty')}</p>
      </div>
    );
  }

  // 按状态分组
  const inProgress = todos.filter((t) => t.status === 'in_progress');
  const pending = todos.filter((t) => t.status === 'pending');
  const completed = todos.filter((t) => t.status === 'completed');

  return (
    <div data-testid="todo-list" className="h-full flex flex-col p-4 space-y-4">
      <h3 className="text-[11px] font-medium text-text-muted uppercase tracking-wider flex items-center justify-between">
        <span>{t('todoList.title')}</span>
        <span className="px-1.5 py-0.5 bg-secondary rounded text-[10px]">{todos.length}</span>
      </h3>

      <div className="flex-1 overflow-y-auto space-y-4">
        {/* 进行中 */}
        {inProgress.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-xs font-medium text-info">
              <span className="w-1.5 h-1.5 rounded-full bg-info animate-pulse" />
              {t('todoList.inProgress')}
            </div>
            <div className="space-y-1">
              {inProgress.map((todo) => (
                <TodoItem key={todo.id} todo={todo} />
              ))}
            </div>
          </div>
        )}

        {/* 待处理 */}
        {pending.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-xs font-medium text-text-muted">
              <span className="w-1.5 h-1.5 rounded-full bg-text-muted" />
              {t('todoList.pending')}
            </div>
            <div className="space-y-1">
              {pending.map((todo) => (
                <TodoItem key={todo.id} todo={todo} />
              ))}
            </div>
          </div>
        )}

        {/* 已完成 */}
        {completed.length > 0 && (
          <div className="space-y-2">
            <div className="flex items-center gap-2 text-xs font-medium text-ok">
              <span className="w-1.5 h-1.5 rounded-full bg-ok" />
              {t('todoList.completed')}
            </div>
            <div className="space-y-1">
              {completed.map((todo) => (
                <TodoItem key={todo.id} todo={todo} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
