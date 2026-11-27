/**
 * ChatPanel 组件
 *
 * 聊天面板，包含消息列表和输入区域
 */

import React, { useRef, useEffect, useLayoutEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores';
import { AgentMode, UserAnswer } from '../../types';
import { MessageList } from './MessageList';
import { InputArea } from './InputArea';
import { SubtaskProgress } from './SubtaskProgress';
import { InlineQuestionCard } from './InlineQuestionCard';
import { HistoryPagerBar } from './HistoryPagerBar';
import './ChatPanel.css';

export interface ChatHistoryPagerProps {
  loadedPages: number;
  totalPages: number;
  loadingMore: boolean;
  onLoadMore: () => void | Promise<void>;
}

interface ChatPanelProps {
  onSendMessage: (content: string) => void;
  onInterrupt: (newInput?: string) => void;
  onSwitchMode: (mode: AgentMode) => void;
  isProcessing: boolean;
  onNewSession: () => Promise<void>;
  onUserAnswer: (requestId: string, answers: UserAnswer[]) => void;
  /** 自会话管理恢复历史后出现；支持分页加载更早消息 */
  historyPager?: ChatHistoryPagerProps | null;
}

function ThinkingIndicator() {
  return (
    <div className="flex justify-start animate-rise">
      <div className="chat-bubble assistant chat-reading-indicator">
        <div className="chat-reading-indicator__dots">
          <span />
          <span />
          <span />
        </div>
      </div>
    </div>
  );
}


function SuggestionCard({ text, onClick }: { text: string; onClick: () => void }) {
  return (
    <button className="chat-suggestion-card" onClick={onClick}>
      <svg className="chat-suggestion-card__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
      </svg>
      <span className="chat-suggestion-card__text">{text}</span>
      <svg className="chat-suggestion-card__arrow" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
      </svg>
    </button>
  );
}


export function ChatPanel({
  onSendMessage,
  onInterrupt,
  onSwitchMode,
  isProcessing,
  onNewSession,
  onUserAnswer,
  historyPager = null,
}: ChatPanelProps) {
  const { t } = useTranslation();
  const { messages, isThinking } = useChatStore();
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const prependScrollSnapRef = useRef<{ sh: number; st: number } | null>(null);
  const wasHistoryLoadingRef = useRef(false);
  const suppressNextScrollToEndRef = useRef(false);
  const [isSending, setIsSending] = React.useState(false);
  const suggestions = [
    t('chat.welcomeSuggestions.journey'),
    t('chat.welcomeSuggestions.skills'),
  ];

  // 跟踪用户是否正在查看历史消息（不在底部）
  const userScrolledUpRef = useRef(false);

  // 检测用户滚动位置
  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    
    // 检查是否在底部（有 40px 的阈值）
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    userScrolledUpRef.current = !atBottom;
    
    // 当滚动到顶部且有更多历史消息时，加载更多
    if (el.scrollTop === 0 && historyPager && historyPager.loadedPages < historyPager.totalPages && !historyPager.loadingMore) {
      void historyPager.onLoadMore();
    }
  }, [historyPager]);

  // 检测鼠标滚轮事件，即使没有滚动条也能触发加载更多
  const handleWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    // 只有向上滚动时才触发
    if (e.deltaY < 0 && historyPager && historyPager.loadedPages < historyPager.totalPages && !historyPager.loadingMore) {
      // 检查是否已经在顶部（没有滚动条时 scrollTop 始终为 0）
      const el = scrollContainerRef.current;
      if (el && el.scrollTop === 0) {
        void historyPager.onLoadMore();
      }
    }
  }, [historyPager]);

  useEffect(() => {
    if (suppressNextScrollToEndRef.current) {
      suppressNextScrollToEndRef.current = false;
      return;
    }
    
    // 只有当用户在底部时才自动滚动
    if (!userScrolledUpRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, isThinking]);

  useLayoutEffect(() => {
    if (!historyPager) {
      wasHistoryLoadingRef.current = false;
      prependScrollSnapRef.current = null;
      return;
    }
    const el = scrollContainerRef.current;
    if (!el) return;

    if (historyPager.loadingMore) {
      if (!wasHistoryLoadingRef.current) {
        prependScrollSnapRef.current = { sh: el.scrollHeight, st: el.scrollTop };
      }
      wasHistoryLoadingRef.current = true;
      return;
    }

    if (wasHistoryLoadingRef.current && prependScrollSnapRef.current) {
      const snap = prependScrollSnapRef.current;
      const delta = el.scrollHeight - snap.sh;
      if (delta > 0) {
        el.scrollTop = snap.st + delta;
        suppressNextScrollToEndRef.current = true;
      }
      prependScrollSnapRef.current = null;
    }
    wasHistoryLoadingRef.current = false;
  }, [historyPager, messages.length]);

  // 包装发送消息函数，添加滚动逻辑
  const handleSendMessage = useCallback((content: string) => {
    setIsSending(true);
    onSendMessage(content);
  }, [onSendMessage]);

  // 当发送消息时强制滚动到底部
  useEffect(() => {
    if (isSending) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
      userScrolledUpRef.current = false;
      setIsSending(false);
    }
  }, [isSending]);

  const handleSuggestion = useCallback(
    (text: string) => handleSendMessage(text),
    [handleSendMessage],
  );

  return (
    <div className="flex flex-col h-full" data-testid="chat-panel">
      <div ref={scrollContainerRef} className="flex-1 overflow-y-auto px-3 py-4" onScroll={handleScroll} onWheel={handleWheel}>
        {historyPager || messages.length > 0 ? (
          <>
            {historyPager && (
              <HistoryPagerBar
                loadedPages={historyPager.loadedPages}
                totalPages={historyPager.totalPages}
                loadingMore={historyPager.loadingMore}
                onLoadMore={historyPager.onLoadMore}
              />
            )}
            {messages.length > 0 ? (
              <>
                <MessageList messages={messages} />
                <SubtaskProgress />
                {/* 内联审批卡片（演进审批 & 权限审批共用） */}
                <InlineQuestionCard onSubmit={onUserAnswer} />
                {/* 思考中指示器 */}
                {isThinking && <ThinkingIndicator />}
              </>
            ) : (
              <div className="flex items-center justify-center h-32">
                <div className="text-text-muted text-sm">
                  {t('connection.loadingConfig')}
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="chat-welcome">
            <img src="/logo.png" alt={t('chat.welcomeLogoAlt')} className="chat-welcome__logo" />
            <h2 className="chat-welcome__heading">{t('chat.welcomeHeading')}</h2>
            <p className="chat-welcome__subtext">
              {t('chat.welcomeSubtext')}
            </p>
            <div className="chat-suggestions">
              {suggestions.map((text) => (
                <SuggestionCard key={text} text={text} onClick={() => handleSuggestion(text)} />
              ))}
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-compose px-3 pb-4">
        <InputArea
          onSubmit={handleSendMessage}
          onInterrupt={onInterrupt}
          onSwitchMode={onSwitchMode}
          isProcessing={isProcessing}
          onNewSession={onNewSession}
        />
      </div>
    </div>
  );
}
