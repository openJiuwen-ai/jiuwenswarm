/**
 * TodoItem 组件
 *
 * 单个任务项显示
 */

import { TodoItem as TodoItemType } from '../../types';
import clsx from 'clsx';

interface TodoItemProps {
  todo: TodoItemType;
}

export function TodoItem({ todo }: TodoItemProps) {
  const getStatusIcon = () => {
    switch (todo.status) {
      case 'pending':
        return (
          <span className="w-4 h-4 rounded border border-border-strong flex items-center justify-center flex-shrink-0" />
        );
      case 'in_progress':
        return (
          <span className="w-4 h-4 rounded border-2 border-info flex items-center justify-center flex-shrink-0 animate-pulse">
            <span className="w-1.5 h-1.5 rounded-full bg-info" />
          </span>
        );
      case 'completed':
        return (
          <span className="w-4 h-4 rounded bg-ok flex items-center justify-center flex-shrink-0">
            <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={3}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
            </svg>
          </span>
        );
      default:
        return null;
    }
  };

  return (
    <div
      data-testid={`todo-item-${todo.id}`}
      data-todo-status={todo.status}
      className={clsx(
        'flex flex-col gap-1 py-1.5 px-2 rounded-md text-sm transition-colors',
        todo.status === 'in_progress' && 'bg-accent-subtle',
        todo.status === 'completed' && 'opacity-60'
      )}
    >
      <div className="flex items-start gap-2">
        {getStatusIcon()}
        <span
          className={clsx(
            'flex-1 leading-tight',
            todo.status === 'completed' && 'line-through text-text-muted',
            todo.status === 'in_progress' && 'text-text-strong',
            todo.status === 'pending' && 'text-text'
          )}
        >
          {todo.status === 'in_progress' && todo.activeForm ? todo.activeForm : todo.content}
        </span>
      </div>
      {/* 任务依赖 */}
      {todo.depends && todo.depends.length > 0 && (
        <div className="text-xs text-text-muted ml-6">
          依赖: {todo.depends.join(', ')}
        </div>
      )}
      {/* 被认领信息 */}
      {todo.claimedBy && (
        <div className="text-xs text-info ml-6">
          被认领: {todo.claimedBy}
        </div>
      )}
    </div>
  );
}
