/**
 * TeamArea 组件
 *
 * 显示团队信息，包括团队名称、成员列表和成员状态
 */

import { useTranslation } from 'react-i18next';
import { useTodoStore } from '../stores';

interface TeamMember {
  id: string;
  member_id: string;
  status: string;
  timestamp: number;
  currentTask?: string;
}

interface TeamAreaProps {
  members: TeamMember[];
}

export function TeamArea({ members }: TeamAreaProps) {
  const { t } = useTranslation();
  const { todos } = useTodoStore();

  // 获取每个成员正在执行的任务
  const getMemberCurrentTask = (memberId: string) => {
    return todos.find(todo => todo.claimedBy === memberId && todo.status === 'in_progress');
  };

  const getStatusColor = (status: TeamMember['status']) => {
    switch (status) {
      case 'ready':
        return 'bg-green-500';
      case 'busy':
        return 'bg-yellow-500';
      case 'restarting':
        return 'bg-blue-500';
      case 'shutdown_requested':
        return 'bg-orange-500';
      case 'shut_down':
        return 'bg-gray-400';
      case 'error':
        return 'bg-red-500';
      case 'unstarted':
      default:
        return 'bg-gray-300';
    }
  };

  const getStatusText = (status: TeamMember['status']) => {
    switch (status) {
      case 'unstarted':
        return t('team.statusUnstarted');
      case 'ready':
        return t('team.statusReady');
      case 'busy':
        return t('team.statusBusy');
      case 'restarting':
        return t('team.statusRestarting');
      case 'shutdown_requested':
        return t('team.statusShutdownRequested');
      case 'shut_down':
        return t('team.statusShutdown');
      case 'error':
        return t('team.statusError');
      default:
        return status;
    }
  };

  const formatTime = (timestamp: number) => {
    const date = new Date(timestamp);
    return date.toLocaleTimeString();
  };

  return (
    <div className="h-full flex flex-col">
      <div className="p-4 flex-1 overflow-hidden flex flex-col">
        <h3 className="text-[11px] font-medium text-text-muted uppercase tracking-wider mb-4">
          {t('team.members')}
        </h3>
        <div className="flex-1 overflow-y-auto space-y-3">
          {members.map((member) => {
            const currentTask = getMemberCurrentTask(member.member_id);
            return (
              <div key={member.id} className="space-y-1">
                <div className="flex items-center gap-2">
                  <span
                    className={`w-2 h-2 rounded-full ${getStatusColor(member.status)}`}
                    title={getStatusText(member.status)}
                  />
                  <span className="text-xs text-text-muted">{member.member_id}</span>
                  <span className="ml-auto text-xs text-text-muted">{formatTime(member.timestamp)}</span>
                </div>
                {currentTask && (
                  <div className="text-xs text-text-muted ml-4 truncate max-w-[120px]">
                    {t('team.executing')}: {currentTask.content}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
