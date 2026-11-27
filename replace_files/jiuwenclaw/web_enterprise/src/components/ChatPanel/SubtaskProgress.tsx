/**
 * SubtaskProgress 组件
 *
 * 显示并行子任务的执行进度
 */

import { useChatStore } from '../../stores';
import { useTranslation } from 'react-i18next';
import clsx from 'clsx';

export function SubtaskProgress() {
  const { t } = useTranslation();
  const { activeSubtasks } = useChatStore();

  // 将 Map 转换为数组并按 index 排序
  const subtasks = Array.from(activeSubtasks.values()).sort(
    (a, b) => a.index - b.index
  );

  if (subtasks.length === 0) {
    return null;
  }

  // 获取总数（从第一个子任务）
  const total = subtasks[0]?.total || subtasks.length;

  // 计算完成数量
  const completedCount = total - subtasks.length;

  return (
    <div className="mx-4 my-2 p-3 bg-accent-subtle rounded-lg border border-border">
      <div className="flex items-center gap-2 mb-2">
        <span className="w-2 h-2 rounded-full bg-info animate-pulse" />
        <span className="text-sm font-medium text-text-strong">
          {t('chatUi.parallelProgress', { completedCount, total })}
        </span>
      </div>
      <div className="space-y-2">
        {subtasks.map((subtask) => (
          <SubtaskItem key={subtask.task_id} subtask={subtask} />
        ))}
      </div>
    </div>
  );
}

interface SubtaskItemProps {
  subtask: {
    task_id: string;
    description: string;
    status: string;
    index: number;
    total: number;
    tool_name?: string;
    tool_count: number;
    message?: string;
    is_parallel: boolean;
  };
}

function SubtaskItem({ subtask }: SubtaskItemProps) {
  const { t } = useTranslation();
  const getStatusIcon = () => {
    switch (subtask.status) {
      case 'starting':
        return (
          <span className="w-3 h-3 rounded-full border-2 border-info animate-pulse flex-shrink-0" />
        );
      case 'tool_call':
        return (
          <span className="w-3 h-3 rounded-full bg-warning animate-pulse flex-shrink-0" />
        );
      case 'tool_result':
        return (
          <span className="w-3 h-3 rounded-full bg-info flex-shrink-0" />
        );
      default:
        return (
          <span className="w-3 h-3 rounded-full border border-border-strong flex-shrink-0" />
        );
    }
  };

  const getStatusText = () => {
    switch (subtask.status) {
      case 'starting':
        return t('chatUi.subtask.starting');
      case 'tool_call':
        return t('chatUi.subtask.toolCall', { tool: subtask.tool_name || t('chatUi.subtask.toolFallback'), count: subtask.tool_count });
      case 'tool_result':
        return subtask.message ? `${subtask.message.slice(0, 50)}...` : t('chatUi.subtask.toolResult');
      default:
        return subtask.status;
    }
  };

  return (
    <div
      className={clsx(
        'flex items-start gap-2 py-1.5 px-2 rounded text-sm',
        subtask.status === 'tool_call' && 'bg-warning/10',
        subtask.status === 'tool_result' && 'bg-info/10'
      )}
    >
      {getStatusIcon()}
      <div className="flex-1 min-w-0">
        <div className="font-medium text-text-strong truncate">
          {t('chatUi.subtask.taskLabel', { index: subtask.index + 1, description: subtask.description })}
        </div>
        <div className="text-text-muted truncate">{getStatusText()}</div>
      </div>
      {subtask.tool_count > 0 && (
        <span className="text-text-muted text-xs px-1.5 py-0.5 bg-secondary rounded">
          {t('chatUi.subtask.callCount', { count: subtask.tool_count })}
        </span>
      )}
    </div>
  );
}
