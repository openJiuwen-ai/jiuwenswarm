/**
 * SessionItem 组件
 *
 * 单个会话项，支持删除
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Session } from '../../types';
import clsx from 'clsx';

interface SessionItemProps {
  session: Session;
  isActive: boolean;
  onClick: () => void;
  onDelete: () => void;
}

/**
 * 格式化时间显示
 */
function formatTime(
  timestamp: string,
  language: string,
  t: (key: string, options?: Record<string, unknown>) => string
): string {
  const date = new Date(timestamp);
  const now = new Date();
  const diff = now.getTime() - date.getTime();

  if (diff < 60000) {
    return t('time.relative.justNow');
  }

  if (diff < 3600000) {
    const minutes = Math.floor(diff / 60000);
    return t('time.relative.minutesAgo', { count: minutes });
  }

  if (diff < 86400000) {
    const hours = Math.floor(diff / 3600000);
    return t('time.relative.hoursAgo', { count: hours });
  }

  if (diff < 604800000) {
    const days = Math.floor(diff / 86400000);
    return t('time.relative.daysAgo', { count: days });
  }

  return date.toLocaleDateString(language, { month: 'short', day: 'numeric' });
}

export function SessionItem({ session, isActive, onClick, onDelete }: SessionItemProps) {
  const { t, i18n } = useTranslation();
  const [showDelete, setShowDelete] = useState(false);
  
  // 优先展示会话标题，缺失时回退到会话 ID 片段
  const preview = session.title?.trim() || session.session_id.slice(0, 8);

  const handleDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    onDelete();
  };

  return (
    <div
      className="relative group"
      onMouseEnter={() => setShowDelete(true)}
      onMouseLeave={() => setShowDelete(false)}
    >
      <button
        onClick={onClick}
        className={clsx(
          'nav-item w-full text-left pr-8',
          isActive && 'active'
        )}
      >
        {/* 会话图标 */}
        <div className={clsx(
          'w-8 h-8 rounded-md flex items-center justify-center flex-shrink-0',
          isActive ? 'bg-accent text-white' : 'bg-secondary text-text-muted'
        )}>
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
          </svg>
        </div>

        {/* 会话信息 */}
        <div className="flex-1 min-w-0">
          <div className={clsx(
            'text-sm font-medium truncate',
            isActive ? 'text-text-strong' : 'text-text'
          )}>
            {preview}
          </div>
          <div className="text-xs text-text-muted truncate">
            {session.updated_at
              ? formatTime(session.updated_at, i18n.language, t)
              : t('sessionSidebar.newSession')}
          </div>
        </div>

        {/* 消息计数 */}
        {session.message_count && session.message_count > 0 && !showDelete && (
          <span className={clsx(
            'text-xs px-1.5 py-0.5 rounded-full',
            isActive ? 'bg-accent-subtle text-accent' : 'bg-secondary text-text-muted'
          )}>
            {session.message_count}
          </span>
        )}
      </button>

      {/* 删除按钮 */}
      {showDelete && (
        <button
          onClick={handleDelete}
          className="absolute right-2 top-1/2 -translate-y-1/2 p-1.5 rounded-md text-text-muted hover:text-danger hover:bg-danger-subtle transition-colors"
          title={t('sessions.delete')}
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
          </svg>
        </button>
      )}
    </div>
  );
}
