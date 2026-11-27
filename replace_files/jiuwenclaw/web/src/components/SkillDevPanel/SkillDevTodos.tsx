/**
 * SkillDevTodos - Todo 列表组件
 */

import { useTranslation } from 'react-i18next';
import type { SkillDevTodo, SkillDevStage } from '../../types/skilldev';

interface SkillDevTodosProps {
  todos: SkillDevTodo[];
  currentStage: SkillDevStage;
}

export function SkillDevTodos({ todos, currentStage: _currentStage }: SkillDevTodosProps) {
  const { t } = useTranslation();

  const getStatusIcon = (status: SkillDevTodo['status']) => {
    switch (status) {
      case 'completed':
        return (
          <svg className="w-4 h-4 text-success" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        );
      case 'in_progress':
        return (
          <div className="w-4 h-4 border-2 border-primary border-t-transparent rounded-full animate-spin" />
        );
      default:
        return (
          <div className="w-4 h-4 border-2 border-border rounded-full" />
        );
    }
  };

  return (
    <div className="p-4 border-b border-border">
      <h3 className="text-sm font-semibold text-text mb-3 flex items-center gap-2">
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
        </svg>
        {t('skilldev.todosTitle')}
      </h3>
      <div className="space-y-2">
        {todos.length === 0 ? (
          <p className="text-xs text-text-muted italic">{t('skilldev.noTodos')}</p>
        ) : (
          todos.map((todo) => (
            <div
              key={todo.id}
              className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
                todo.status === 'in_progress'
                  ? 'bg-primary/10 border border-primary/20'
                  : todo.status === 'completed'
                  ? 'bg-success/5'
                  : 'bg-hover'
              }`}
            >
              {getStatusIcon(todo.status)}
              <span
                className={`flex-1 ${
                  todo.status === 'completed'
                    ? 'text-text-muted line-through'
                    : todo.status === 'in_progress'
                    ? 'text-text font-medium'
                    : 'text-text'
                }`}
              >
                {todo.label}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
