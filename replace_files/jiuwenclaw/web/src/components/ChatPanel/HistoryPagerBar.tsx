import { useTranslation } from 'react-i18next';
import { useState, useEffect } from 'react';

export interface HistoryPagerBarProps {
  loadedPages: number;
  totalPages: number;
  loadingMore: boolean;
  onLoadMore: () => void;
}

export function HistoryPagerBar({
  loadedPages,
  totalPages,
  loadingMore,
  onLoadMore,
}: HistoryPagerBarProps) {
  const { t } = useTranslation();
  const hasMore = loadedPages < totalPages;
  const [showLoadedMessage, setShowLoadedMessage] = useState(false);

  // 监听 loadingMore 状态变化，当从 true 变为 false 时，显示加载完成消息
  useEffect(() => {
    if (!loadingMore && loadedPages > 0) {
      setShowLoadedMessage(true);
      // 3秒后关闭消息
      const timer = setTimeout(() => {
        setShowLoadedMessage(false);
      }, 3000);
      return () => clearTimeout(timer);
    }
  }, [loadingMore, loadedPages]);

  // 当开始加载新内容时，隐藏已加载消息
  useEffect(() => {
    if (loadingMore) {
      setShowLoadedMessage(false);
    }
  }, [loadingMore]);

  const handleClick = () => {
    if (hasMore && !loadingMore) {
      void onLoadMore();
    }
  };

  return (
    <div 
      className="history-pager-bar mb-3 rounded-lg border border-white/10 bg-secondary/50 px-3 py-2.5 flex flex-col items-center justify-center gap-2 text-sm cursor-pointer"
      onClick={handleClick}
      title={t('chat.historyPager.clickOrScrollToLoadMore')}
    >
      {/* 加载完成消息 */}
      {showLoadedMessage && (
        <span className="text-text-muted tabular-nums animate-fade-in">
          {t('chat.historyPager.loadedOfTotal', { loaded: loadedPages, total: totalPages })}
        </span>
      )}
      
      {/* 加载状态或提示文本 */}
      {loadingMore ? (
        <div className="flex justify-center items-center py-2">
          <svg className="animate-spin -ml-1 mr-2 h-4 w-4 text-text-muted" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
          </svg>
        </div>
      ) : (
        hasMore ? (
          <span className="text-xs text-text-muted">
            {t('chat.historyPager.clickOrScrollToLoadMore')}
          </span>
        ) : (
          <span className="text-xs text-text-muted">{t('chat.historyPager.allLoaded')}</span>
        )
      )}
    </div>
  );
}
