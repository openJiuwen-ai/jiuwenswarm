/**
 * ChatPanel 组件
 *
 * 聊天面板，包含消息列表和输入区域
 */

import React, { useRef, useEffect, useLayoutEffect, useCallback, useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { ArrowRight, CheckCircle2, ClipboardList, Copy, Info, LoaderCircle, Share2, Sparkles, X } from 'lucide-react';
import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { useChatStore, useHarnessStore, useSessionStore, useTodoStore } from '../../stores';
import { AgentMode, MediaItem, Message, UserAnswer, type ProjectInfo } from '../../types';
import type { HumanShareCommand } from '../../stores/sessionStore';
import { MessageList } from './MessageList';
import { ContextCompressionLines } from './MessageItem';
import { InputArea, type InputAreaHandle } from './InputArea';
import chatIcon from '../../assets/chat.svg';
import expandIcon from '../../assets/expand.svg';
import lineUpIcon from '../../assets/lineUp.svg';
import loadSendIcon from '../../assets/load-send.svg';
import editIcon from '../../assets/edit.svg';
import deleteIcon from '../../assets/delete.svg';
import moveIcon from '../../assets/move.svg';
import restartIcon from '../../assets/restart.svg';
import { SubtaskProgress } from './SubtaskProgress';
import { InlineQuestionCard } from './InlineQuestionCard';
import { InteractionSlot } from '../InteractionSlot';
import { GoalBar } from '../GoalBar';
import { HarnessProgressBar } from './HarnessProgressBar';
import { AgentTeamActivityCard } from './TeamEventGroupDisplay';
import { isTeamActivityMessage, parseTeamEventMessage } from './teamEventUtils';
import { isTeamLeaderMember, type TeamMemberIdentity } from '../../utils/teamMemberAvatar';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import welcomeBanner from '../../assets/home-banner-workswarm.png';
import './ChatPanel.css';
import { CodeChangesCard } from '../../features/code-mode/CodeChangesCard';
import { useCodeTurnDiffHistory } from '../../features/code-mode/useCodeTurnDiffHistory';
import { turnDiffKey } from '../../features/code-mode/turnChangeState';
import type { CodeReviewTarget } from '../../features/code-mode/types';
import {
  canLoadOlderHistory,
  shouldShowHistoryRetry,
} from '../../features/historyPagination';
import {
  DESKTOP_FILE_DRAG_EVENT,
  DESKTOP_LOCAL_FILES_EVENT,
  isDesktopShell,
  normalizePicks,
  registerDesktopLocalFilesConsumer,
  type DesktopLocalFilesEventDetail,
  type LocalFilePick,
} from '../../features/workspace/localFilePicker';
import { useDesktopLocalFilePickerReady } from '../../hooks';

export interface ChatHistoryPagerProps {
  loadedPages: number;
  totalPages: number;
  loadingMore: boolean;
  prepending?: boolean;
  retryAvailable?: boolean;
  onLoadMore: () => void | Promise<void>;
}

interface ChatPanelProps {
  onSendMessage: (content: string, mediaItems?: MediaItem[]) => void;
  onPersistMedia: (content: string, mediaItems: MediaItem[]) => Promise<{
    content?: string;
    query?: string;
    media_items?: Record<string, unknown>[];
    files?: Record<string, unknown>;
  }>;
  onPersistDocuments: (content: string, mediaItems: MediaItem[]) => Promise<{
    content?: string;
    query?: string;
    media_items?: Record<string, unknown>[];
    files?: Record<string, unknown>;
  }>;
  onInterrupt: (newInput?: string) => void;
  onCancel: () => void;
  onSwitchMode: (mode: AgentMode) => void;
  isProcessing: boolean;
  onUserAnswer: (requestId: string, answers: UserAnswer[], source?: string) => void;
  onExportShare?: () => void | Promise<void>;
  isExportingShare?: boolean;
  canExportShare?: boolean;
  sessionTitle?: string;
  sessionProjectName?: string;
  sessionProject?: ProjectInfo | null;
  /** 自会话管理恢复历史后出现；支持分页加载更早消息 */
  historyPager?: ChatHistoryPagerProps | null;
  /** 历史会话首屏恢复中：保持聊天布局，避免短暂退回欢迎态 */
  isHistoryRestoring?: boolean;
  /** 右侧面板展开状态：展开时隐藏对话框上方的活跃成员 */
  teamAreaExpanded?: boolean;
  autoFocusKey?: string | null;
  /** 跳转到技能管理页 */
  onNavigateToSkills?: () => void;
  /** 切换右侧紧缩面板展开状态 */
  onToggleTeamArea?: (expanded: boolean) => void;
  /** 打开右侧面板并切换到代码审核 Tab */
  onOpenCodeReview?: (target: CodeReviewTarget) => void;
  permissionsEnabled: boolean;
  onSavePermission: (updates: Record<string, string>) => Promise<void>;
  /** Goal（持续目标）控制，见 GoalBar 组件 */
  onSetGoal?: (sessionId: string, objective: string) => void;
  onPauseGoal?: (sessionId: string) => void;
  onResumeGoal?: (sessionId: string) => void;
  onClearGoal?: (sessionId: string) => void;
  /** 目标 active 但当前无处理中任务时，消息入队后主动排空一次，见 InputArea.tsx 对应调用点 */
  onDrainTaskQueueIfIdle?: (sessionId: string) => void;
}

// 邀请指令只对 human_agent 成员存在（见 upsertHumanShareCommandFromEvent 的
// mode === 'human' 闸门），所以这两处直接断言身份，不依赖成员名册是否已到齐。
const HUMAN_SHARE_IDENTITY: TeamMemberIdentity = { role: 'human_agent' };

function SuggestionCard({ text, onClick }: { text: string; onClick: () => void }) {
  return (
    <button className="chat-suggestion-card" data-testid="chat-panel-welcome-suggestion" data-variant={text} onClick={onClick}>
      <Sparkles className="chat-suggestion-card__icon" strokeWidth={2} />
      <span className="chat-suggestion-card__text">{text}</span>
      <ArrowRight className="chat-suggestion-card__arrow" strokeWidth={2} />
    </button>
  );
}

function InterruptResultBubble() {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const interruptResult = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.interruptResult ?? null);
  const message = interruptResult?.message?.trim();

  if (!message || interruptResult?.success) {
    return null;
  }

  return (
    <div
      className="chat-interrupt-bubble chat-interrupt-bubble--error"
      role="alert"
      data-testid="chat-panel-interrupt-result"
    >
      {message}
    </div>
  );
}

function ActiveTeamGroupEntry({ isProcessing, teamAreaExpanded }: { isProcessing: boolean; teamAreaExpanded?: boolean }) {
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const messages = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.messages ?? []);
  const mode = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.mode ?? 'agent');
  const teamHistoryMessages = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamHistoryMessages ?? []);
  const teamMemberExecutionEvents = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamMemberExecutionEvents ?? []);
  const teamTaskEvents = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);
  const teamTasks = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTasks ?? []);
  const teamMembers = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamMembers ?? []);
  const todos = useTodoStore((s) => s.runtimes[activeSessionId ?? '']?.todos ?? []);
  const activeTeamMessages = useMemo(
    () => getActiveTeamMessages(teamHistoryMessages, messages),
    [teamHistoryMessages, messages]
  );
  const hasVisibleMembers = teamMembers.some(
    (m) => m.member_id && m.member_id !== 'user' && !isTeamLeaderMember(m.member_id)
  );

  if (mode !== 'team' || !hasVisibleMembers || teamAreaExpanded) {
    return null;
  }

  return (
    <AgentTeamActivityCard
      messages={activeTeamMessages}
      isProcessing={isProcessing}
      tasks={teamTasks}
      taskEvents={teamTaskEvents}
      todos={todos}
      executionEvents={teamMemberExecutionEvents}
    />
  );
}

/** 单 Agent 模式的消息队列卡片，展示在输入框上方 */
function AgentActivityCard({ isProcessing: _isProcessing, onSendTask }: { isProcessing: boolean; onSendTask?: (content: string, mediaItems?: MediaItem[]) => void }) {
  const [expanded, setExpanded] = useState(true);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [dragOverIndex, setDragOverIndex] = useState<number | null>(null);
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const mode = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.mode ?? 'agent');
  const taskQueue = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.taskQueue ?? []);
  const queuePaused = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.queuePaused ?? false);
  const removeFromTaskQueue = useChatStore((s) => s.removeFromTaskQueue);
  const reorderTaskQueue = useChatStore((s) => s.reorderTaskQueue);
  const setQueuePaused = useChatStore((s) => s.setQueuePaused);
  const setInputValue = useChatStore((s) => s.setInputValue);

  const isAgentMode = mode === 'agent';

  // 有等待任务时自动展开
  useEffect(() => {
    if (taskQueue.length > 0) {
      setExpanded(true);
    }
  }, [taskQueue.length]);

  if (!isAgentMode || taskQueue.length === 0) {
    return null;
  }

  const handleResume = (e: React.MouseEvent) => {
    e.stopPropagation();
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    setQueuePaused(sid, false);
    // 触发下一条队列任务
    const runtime = useChatStore.getState().getRuntime(sid);
    const nextTask = runtime?.taskQueue[0];
    if (nextTask) {
      removeFromTaskQueue(sid, nextTask.id);
      onSendTask?.(nextTask.content, nextTask.mediaItems);
    }
  };

  const handleRemoveTask = (e: React.MouseEvent, taskId: string) => {
    e.stopPropagation();
    const sid = useChatStore.getState().activeSessionId;
    if (sid) {
      removeFromTaskQueue(sid, taskId);
    }
  };

  const handleEditTask = (
    e: React.MouseEvent,
    taskId: string,
    content: string,
    mediaItemCount = 0,
  ) => {
    e.stopPropagation();
    const sid = useChatStore.getState().activeSessionId;
    if (sid) {
      // Editing restores only the text into the input; attachments cannot follow
      // and will be removed together with the task — confirm first.
      if (
        mediaItemCount > 0 &&
        !window.confirm(t('chat.editTaskDropAttachments', { count: mediaItemCount }))
      ) {
        return;
      }
      setInputValue(sid, content);
      removeFromTaskQueue(sid, taskId);
      window.dispatchEvent(new CustomEvent('chat-input-sync', { detail: { sessionId: sid, value: content } }));
    }
  };

  const handleSendTask = (
    e: React.MouseEvent,
    taskId: string,
    content: string,
    mediaItems?: MediaItem[],
  ) => {
    e.stopPropagation();
    const sid = useChatStore.getState().activeSessionId;
    if (sid) {
      removeFromTaskQueue(sid, taskId);
    }
    onSendTask?.(content, mediaItems);
  };

  const handleDragStart = (index: number) => {
    setDragIndex(index);
  };

  const handleDragOver = (e: React.DragEvent, index: number) => {
    e.preventDefault();
    setDragOverIndex(index);
  };

  const handleDrop = (index: number) => {
    if (dragIndex === null || dragIndex === index) {
      setDragIndex(null);
      setDragOverIndex(null);
      return;
    }
    const sid = useChatStore.getState().activeSessionId;
    if (sid) {
      reorderTaskQueue(sid, dragIndex, index);
    }
    setDragIndex(null);
    setDragOverIndex(null);
  };

  const handleDragEnd = () => {
    setDragIndex(null);
    setDragOverIndex(null);
  };

  return (
    <div className="chat-active-team-group animate-rise" data-testid="chat-panel-task-queue">
      <div className="team-event-group team-event-group--activity">
        <button
          type="button"
          className="team-event-group-summary"
          data-testid="chat-panel-task-queue-header"
          onClick={() => setExpanded(prev => !prev)}
          aria-expanded={expanded}
        >
          <span className="team-event-group-summary__main">
            <span className="team-event-group-summary__title">{t('chatUi.messageQueue')}</span>
            {queuePaused && (
              <span data-testid="chat-panel-task-queue-paused-badge" style={{ display: 'inline-flex', alignItems: 'center', gap: '5px', marginLeft: '8px' }}>
                <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: 'var(--color-chat-paused)', flexShrink: 0 }} />
                <span style={{ fontSize: '13px', color: 'var(--color-text-secondary)' }}>{t('chat.paused')}</span>
              </span>
            )}
          </span>
          {queuePaused && (
            <span
              role="button"
              tabIndex={0}
              className="team-event-group-summary__activity"
              data-testid="chat-panel-task-queue-resume"
              style={{ display: 'inline-flex', alignItems: 'center', gap: '4px', marginLeft: 'auto', justifyContent: 'end', flexShrink: 0, cursor: 'pointer' }}
              onClick={handleResume}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); handleResume(e as unknown as React.MouseEvent); } }}
            >
              <img src={restartIcon} alt="" className="w-3.5 h-3.5" />
              {t('chat.resume')}
            </span>
          )}
        </button>
        {expanded && (
          <div className="team-event-group-list team-event-group-list--activity">
            {taskQueue.map((task, index) => (
              <div
                key={task.id}
                className="team-event-group-row team-event-group-row--activity"
                data-testid="chat-panel-task-queue-item"
                data-variant={task.id}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  gap: '8px',
                  opacity: dragIndex === index ? 0.4 : 1,
                  background: dragOverIndex === index ? 'var(--color-surface-hover)' : 'transparent',
                }}
                onDragOver={(e) => handleDragOver(e, index)}
                onDrop={() => handleDrop(index)}
                onDragEnd={handleDragEnd}
              >
                <div className="team-event-group-row__main" style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                  {/* 拖动图标：所有任务可拖，悬浮显示 */}
                  <img
                    src={moveIcon}
                    alt=""
                    draggable
                    onDragStart={() => handleDragStart(index)}
                    className="queue-drag-handle"
                    data-testid="chat-panel-task-queue-item-drag"
                    title={t('chat.dragTask')}
                  />
                  <div className="team-event-group-row__avatar" style={{ display: 'flex', alignItems: 'center' }}>
                    <img src={lineUpIcon} alt="" className="w-4 h-4" />
                  </div>
                  <span className="team-event-group-row__member" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {task.content}
                  </span>
                  {(task.mediaItems?.length ?? 0) > 0 && (
                    <span
                      title={(task.mediaItems ?? [])
                        .map((item) => item.filename)
                        .filter(Boolean)
                        .join('\n')}
                      data-testid="chat-panel-task-queue-item-attachment-count"
                      style={{
                        flexShrink: 0,
                        fontSize: '12px',
                        color: 'var(--color-text-secondary)',
                        background: 'var(--color-surface-hover)',
                        borderRadius: '6px',
                        padding: '0 6px',
                      }}
                    >
                      📎{task.mediaItems?.length}
                    </span>
                  )}
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: '4px', flexShrink: 0 }}>
                  <button
                    type="button"
                    className="chat-input-task-action chat-input-task-action--send"
                    data-testid="chat-panel-task-queue-item-send"
                    title={t('chat.sendTask')}
                    onClick={(e) => handleSendTask(e, task.id, task.content, task.mediaItems)}
                  >
                    <img src={loadSendIcon} alt="" className="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    className="chat-input-task-action chat-input-task-action--edit"
                    data-testid="chat-panel-task-queue-item-edit"
                    title={t('chat.editTask')}
                    onClick={(e) => handleEditTask(e, task.id, task.content, task.mediaItems?.length ?? 0)}
                  >
                    <img src={editIcon} alt="" className="w-3 h-3" />
                  </button>
                  <button
                    type="button"
                    className="chat-input-task-action chat-input-task-action--delete"
                    data-testid="chat-panel-task-queue-item-delete"
                    title={t('chat.removeTask')}
                    onClick={(e) => handleRemoveTask(e, task.id)}
                  >
                    <img src={deleteIcon} alt="" className="w-3 h-3" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function getActiveTeamMessages(historyMessages: Message[], messages: Message[]): Message[] {
  const seen = new Set<string>();
  return [...historyMessages, ...messages]
    .filter(isTeamActivityMessage)
    .filter((message) => {
      const key = getTeamMessageIdentity(message);
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
}

function getTeamMessageIdentity(message: Message): string {
  const event = parseTeamEventMessage(message);
  if (!event) {
    return message.id || `${message.timestamp}:${message.content}`;
  }
  return [
    'team',
    event.type,
    event.messageId,
    event.fromMember,
    event.toMember || '',
    event.timestamp || '',
    event.content,
  ].join(':');
}

function WelcomeHeading() {
  const { i18n } = useTranslation();
  const isZh = i18n.language.startsWith('zh');

  if (isZh) {
    return (
      <>
        WorkSwarm 轻松解决工作每个问题！
      </>
    );
  }

  return (
    <>
      WorkSwarm makes work easier!
    </>
  );
}

function getShareExportTitle(
  t: TFunction,
  isExportingShare: boolean,
  canExportShare: boolean
): string {
  if (isExportingShare) {
    return t('share.exporting');
  }
  if (!canExportShare) {
    return t('share.exportUnavailable');
  }
  return t('share.export');
}

function getHumanShareStatusLabel(command: HumanShareCommand, t: TFunction): string {
  if (command.status === 'joined') return t('humanShare.status.joined');
  if (command.status === 'left') return t('humanShare.status.left');
  return t('humanShare.status.pending');
}

function getHumanShareStatusClass(command: HumanShareCommand): string {
  if (command.status === 'joined') return 'human-share-modal__badge human-share-modal__badge--joined';
  if (command.status === 'left') return 'human-share-modal__badge human-share-modal__badge--left';
  return 'human-share-modal__badge';
}

function HumanSharePanel({
  commands,
  onClose,
}: {
  commands: HumanShareCommand[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [copiedKey, setCopiedKey] = React.useState<string | null>(null);
  const sortedCommands = useMemo(
    () => [...commands].sort((a, b) => a.memberName.localeCompare(b.memberName)),
    [commands]
  );
  const joinedCount = sortedCommands.filter((command) => command.status === 'joined').length;
  const exitCommand =
    sortedCommands.find((command) => command.exitCommand)?.exitCommand ||
    (() => {
      const commandWithSessionRef = sortedCommands.find((command) => command.sessionRef);
      return commandWithSessionRef?.sessionRef ? `/exit ${commandWithSessionRef.sessionRef}` : '';
    })();
  const allJoined = sortedCommands.length > 0 && joinedCount === sortedCommands.length;

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  const copyText = useCallback(async (key: string, text: string) => {
    if (!text) return;
    await navigator.clipboard.writeText(text);
    setCopiedKey(key);
    window.setTimeout(() => {
      setCopiedKey((current) => current === key ? null : current);
    }, 1200);
  }, []);

  return createPortal(
    <div className="human-share-modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="human-share-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="human-share-title"
        data-testid="chat-panel-human-share-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="human-share-modal__header" data-testid="chat-panel-human-share-modal-header">
          <div>
            <div className="human-share-modal__title-row">
              <h2 id="human-share-title" className="human-share-modal__title" data-testid="chat-panel-human-share-modal-title">{t('humanShare.title')}</h2>
            </div>
            <p className="human-share-modal__summary" data-testid="chat-panel-human-share-modal-summary">
              {allJoined
                ? t('humanShare.allJoined', { count: sortedCommands.length })
                : t('humanShare.waiting', { joined: joinedCount, total: sortedCommands.length })}
            </p>
          </div>
          <button type="button" className="human-share-modal__close" data-testid="chat-panel-human-share-modal-close" onClick={onClose} aria-label={t('common.close')}>
            <X size={18} />
          </button>
        </div>

        <div className="human-share-modal__body" data-testid="chat-panel-human-share-modal-member-list">
          <div className="human-share-modal__notice" role="note" data-testid="chat-panel-human-share-modal-notice">
            <Info size={18} strokeWidth={2.4} />
            <span>{t('humanShare.instructionHint')}</span>
          </div>
          {sortedCommands.map((command) => {
            const displayName = command.displayName || command.memberName;
            const copied = copiedKey === `join:${command.memberName}`;
            const shouldShowJoinCommand = command.status !== 'joined' && Boolean(command.joinCommand);
            return (
              <section key={`${command.sessionId}:${command.memberName}`} className="human-share-modal__item" data-testid="chat-panel-human-share-modal-member" data-variant={command.memberName}>
                <div className="human-share-modal__member" data-testid="chat-panel-human-share-modal-member-info">
                  <TeamMemberAvatar
                    member={command.memberName}
                    identity={HUMAN_SHARE_IDENTITY}
                    className="human-share-modal__avatar"
                  />
                  <div className="human-share-modal__member-copy">
                    <div className="human-share-modal__member-name">{displayName}</div>
                    {displayName !== command.memberName && (
                      <div className="human-share-modal__member-id">{command.memberName}</div>
                    )}
                  </div>
                  <span className={getHumanShareStatusClass(command)} data-testid="chat-panel-human-share-modal-member-status" data-variant={command.status}>
                    {getHumanShareStatusLabel(command, t)}
                  </span>
                </div>
                {shouldShowJoinCommand ? (
                  <div className="human-share-modal__command-row" data-testid="chat-panel-human-share-modal-member-join" data-variant="pending">
                    <code className="human-share-modal__command" data-testid="chat-panel-human-share-modal-member-join-command">{command.joinCommand}</code>
                    <button
                      type="button"
                      className="human-share-modal__copy"
                      data-testid="chat-panel-human-share-modal-member-copy"
                      onClick={() => void copyText(`join:${command.memberName}`, command.joinCommand)}
                    >
                      {copied ? <CheckCircle2 size={15} /> : <Copy size={15} />}
                      <span>{copied ? t('humanShare.copied') : t('humanShare.copy')}</span>
                    </button>
                  </div>
                ) : (
                  <div
                    className={`human-share-modal__command-note ${
                      command.status === 'joined'
                        ? 'human-share-modal__command-note--joined'
                        : 'human-share-modal__command-note--pending'
                    }`}
                    data-testid="chat-panel-human-share-modal-member-note"
                    data-variant={command.status === 'joined' ? 'joined' : 'pending'}
                  >
                    {command.status === 'joined' ? <CheckCircle2 size={15} /> : <ClipboardList size={15} />}
                    <span>
                      {command.status === 'joined'
                        ? t('humanShare.joinedNote')
                        : t('humanShare.commandPending')}
                    </span>
                  </div>
                )}
              </section>
            );
          })}

          {exitCommand && (
            <section className="human-share-modal__exit" data-testid="chat-panel-human-share-modal-exit">
              <div className="human-share-modal__exit-title" data-testid="chat-panel-human-share-modal-exit-title">{t('humanShare.exitTitle')}</div>
              <div className="human-share-modal__command-row">
                <code className="human-share-modal__command" data-testid="chat-panel-human-share-modal-exit-command">{exitCommand}</code>
                <button
                  type="button"
                  className="human-share-modal__copy"
                  data-testid="chat-panel-human-share-modal-exit-copy"
                  onClick={() => void copyText('exit', exitCommand)}
                >
                  {copiedKey === 'exit' ? <CheckCircle2 size={15} /> : <Copy size={15} />}
                  <span>{copiedKey === 'exit' ? t('humanShare.copied') : t('humanShare.copy')}</span>
                </button>
              </div>
            </section>
          )}
        </div>
      </section>
    </div>,
    document.body
  );
}

function HumanShareCard({
  commands,
  onShare,
}: {
  commands: HumanShareCommand[];
  onShare: () => void;
}) {
  const { t } = useTranslation();
  const sortedCommands = useMemo(
    () => [...commands].sort((a, b) => a.memberName.localeCompare(b.memberName)),
    [commands]
  );
  const joinedCount = sortedCommands.filter((command) => command.status === 'joined').length;
  const pendingCount = sortedCommands.filter((command) => command.status !== 'joined').length;
  // 保留整条 command：头像要按 member_id 解析（人类成员才认得出人类头像，
  // 传展示名会查不到名册、退回哈希插画，还会和弹窗里同一个人对不上），
  // 名字才用 displayName。
  const previewCommands = sortedCommands.slice(0, 3);

  if (sortedCommands.length === 0) {
    return null;
  }

  return (
    <section className="human-share-card" data-testid="chat-panel-human-share-card">
      <div className="human-share-card__icon" aria-hidden="true" data-testid="chat-panel-human-share-card-icon">
        <ClipboardList size={18} strokeWidth={2} />
      </div>
      <div className="human-share-card__content" data-testid="chat-panel-human-share-card-content">
        <div className="human-share-card__title" data-testid="chat-panel-human-share-card-title">{t('humanShare.cardTitle')}</div>
        <div className="human-share-card__summary" data-testid="chat-panel-human-share-card-summary">
          {t('humanShare.cardSummary', {
            pending: pendingCount,
            joined: joinedCount,
            total: sortedCommands.length,
          })}
        </div>
        <div className="human-share-card__members" data-testid="chat-panel-human-share-card-members">
          {previewCommands.map((command) => (
            <span key={command.memberName} className="human-share-card__member-pill" data-testid="chat-panel-human-share-card-member-pill" data-variant={command.memberName}>
              <TeamMemberAvatar
                member={command.memberName}
                identity={HUMAN_SHARE_IDENTITY}
                className="human-share-card__avatar"
              />
              <span>{command.displayName || command.memberName}</span>
            </span>
          ))}
          {sortedCommands.length > previewCommands.length ? (
            <span className="human-share-card__more" data-testid="chat-panel-human-share-card-more">+{sortedCommands.length - previewCommands.length}</span>
          ) : null}
        </div>
      </div>
      <button
        type="button"
        className="human-share-card__button"
        data-testid="chat-panel-human-share-card-trigger"
        onClick={onShare}
      >
        <Share2 size={15} strokeWidth={2} />
        <span>{t('humanShare.shareButton')}</span>
      </button>
    </section>
  );
}

const SCROLL_BOTTOM_THRESHOLD_PX = 40;
const LOAD_OLDER_THRESHOLD_PX = 8;
const VISIBILITY_RESTORE_SCROLL_SUPPRESS_MS = 300;

function isScrollAtBottom(el: HTMLDivElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

function scrollToBottom(el: HTMLDivElement): void {
  el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
}

export function ChatPanel({
  onSendMessage,
  onPersistMedia,
  onPersistDocuments,
  onInterrupt,
  onCancel,
  onSwitchMode,
  isProcessing,
  onUserAnswer,
  onExportShare,
  isExportingShare = false,
  canExportShare = false,
  sessionTitle,
  sessionProjectName,
  sessionProject = null,
  historyPager = null,
  isHistoryRestoring = false,
  teamAreaExpanded = false,
  autoFocusKey = null,
  onNavigateToSkills,
  onToggleTeamArea,
  onOpenCodeReview,
  permissionsEnabled,
  onSavePermission,
  onSetGoal,
  onPauseGoal,
  onResumeGoal,
  onClearGoal,
  onDrainTaskQueueIfIdle,
}: ChatPanelProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const messages = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.messages ?? []);
  const isThinking = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.isThinking ?? false);
  const toolExecutionOrder = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.toolExecutionOrder ?? []);
  const contextCompressionRuntime = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.contextCompressionRuntime);
  const contextCompressionSummary = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.contextCompressionSummary);
  const mode = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.mode ?? 'agent');
  const hasHarnessProgress = useHarnessStore((s) => (
    mode === 'auto_harness' && (s.runtimes[activeSessionId ?? '']?.stageResults.length ?? 0) > 0
  ));
  const teamHumanShareCommands = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamHumanShareCommands ?? []);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const inputAreaRef = useRef<InputAreaHandle>(null);
  const desktopFileDropAcceptUntilRef = useRef(0);
  const lastConsumedDesktopDropIdRef = useRef<string | null>(null);
  const historyLayoutSnapshotRef = useRef<{
    sessionId: string;
    loadedPages: number;
    scrollHeight: number;
    scrollTop: number;
  } | null>(null);
  const suppressNextScrollToEndRef = useRef(false);
  const stickToBottomUntilStableRef = useRef(false);
  const [isSending, setIsSending] = React.useState(false);
  const isDesktopAttachmentDropEnabled = useDesktopLocalFilePickerReady();
  const hasTimelineContent = messages.length > 0 || toolExecutionOrder.length > 0;
  const hasConversation = Boolean(isHistoryRestoring || historyPager || hasTimelineContent);
  const historyLoadedPages = historyPager?.loadedPages ?? 0;
  const historyTotalPages = historyPager?.totalPages ?? 0;
  const historyLoadingMore = historyPager?.loadingMore ?? false;
  const historyPrepending = historyPager?.prepending ?? false;
  const historyRetryAvailable = historyPager?.retryAvailable ?? false;
  const historyOnLoadMore = historyPager?.onLoadMore;
  const hasHistoryPager = Boolean(historyPager);
  const historyLoadMoreState = {
    loadedPages: historyLoadedPages,
    totalPages: historyTotalPages,
    loadingMore: historyLoadingMore,
    prepending: historyPrepending,
  };
  const canRequestOlderHistory = Boolean(
    historyOnLoadMore && canLoadOlderHistory(historyLoadMoreState)
  );
  const showHistoryRetry = Boolean(
    historyOnLoadMore &&
      shouldShowHistoryRetry({
        ...historyLoadMoreState,
        retryAvailable: historyRetryAvailable,
      })
  );
  const chatContentClassName = hasConversation
    ? `chat-content${mode === 'team' ? ' chat-content--team' : ''}`
    : 'chat-content chat-content--welcome';
  const suggestions = [
    t('chat.welcomeSuggestions.journey'),
    t('chat.welcomeSuggestions.skills'),
  ];
  const shouldShowChatHeader = hasConversation;
  const shareExportTitle = getShareExportTitle(t, isExportingShare, canExportShare);
  const shouldShowShareExport = Boolean(onExportShare);
  const shouldShowHumanShare = mode === 'team' && teamHumanShareCommands.length > 0;
  const [humanShareOpen, setHumanShareOpen] = React.useState(false);
  const {
    turnsByMessageId: codeTurnsByMessageId,
    loading: codeTurnHistoryLoading,
    reload: reloadCodeTurnHistory,
    latestTurnKey: latestCodeTurnKey,
    turnChangeOperation,
    turnChangeError,
    turnChangeNotice,
    discardLatestTurn,
    redoLatestTurn,
  } = useCodeTurnDiffHistory({
    project: sessionProject,
    sessionId: activeSessionId,
    isProcessing,
    messages,
  });
  const renderCodeChangesAfterMessage = useCallback((message: Message) => {
    const turns = codeTurnsByMessageId.get(message.id);
    if (!turns?.length) return null;
    return turns.map(turn => {
      const turnKey = turnDiffKey(turn);
      const isLatest = turnKey === latestCodeTurnKey;
      return (
        <CodeChangesCard
          key={turnKey}
          diff={turn}
          refreshing={codeTurnHistoryLoading}
          isLatest={isLatest}
          isProcessing={isProcessing}
          operation={isLatest ? turnChangeOperation?.action ?? null : null}
          operationError={turnChangeError?.turnKey === turnKey ? turnChangeError.message : null}
          onRefresh={() => void reloadCodeTurnHistory()}
          onReview={target => onOpenCodeReview?.(target)}
          onDiscard={() => void discardLatestTurn()}
          onRedo={() => void redoLatestTurn()}
        />
      );
    });
  }, [
    codeTurnHistoryLoading,
    codeTurnsByMessageId,
    discardLatestTurn,
    isProcessing,
    latestCodeTurnKey,
    onOpenCodeReview,
    redoLatestTurn,
    reloadCodeTurnHistory,
    turnChangeError,
    turnChangeOperation,
  ]);

  // 跟踪用户是否正在查看历史消息（不在底部）
  const userScrolledUpRef = useRef(false);
  // 跟踪上一个 sessionId，切换 session 时需要恢复或重置滚动状态
  const lastSessionIdRef = useRef<string>(activeSessionId ?? '');
  // 记忆每个访问过的 session 的滚动位置
  const sessionScrollTopMapRef = useRef<Map<string, number>>(new Map());
  // 记录 tab 从隐藏恢复为可见的时间，用于抑制恢复后的自动滚底
  const visibilityRestoredAtRef = useRef<number>(0);

  const rememberSessionScrollTop = useCallback((sessionId: string, el: HTMLDivElement) => {
    if (sessionId) {
      sessionScrollTopMapRef.current.set(sessionId, el.scrollTop);
    }
  }, []);

  const updateHistoryLayoutSnapshot = useCallback((sessionId: string, el: HTMLDivElement) => {
    historyLayoutSnapshotRef.current = {
      sessionId,
      loadedPages: historyLoadedPages,
      scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop,
    };
  }, [historyLoadedPages]);

  const restoreSessionScrollTop = useCallback((sessionId: string, el: HTMLDivElement): boolean => {
    const savedScrollTop = sessionScrollTopMapRef.current.get(sessionId);
    if (savedScrollTop === undefined) {
      return false;
    }

    el.scrollTop = savedScrollTop;
    const atBottom = isScrollAtBottom(el);
    userScrolledUpRef.current = !atBottom;
    stickToBottomUntilStableRef.current = atBottom;
    updateHistoryLayoutSnapshot(sessionId, el);
    return true;
  }, [updateHistoryLayoutSnapshot]);

  // 检测用户滚动位置
  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;

    const atBottom = isScrollAtBottom(el);
    userScrolledUpRef.current = !atBottom;
    if (!atBottom) {
      stickToBottomUntilStableRef.current = false;
    }

    const currentSessionId = activeSessionId ?? '';
    rememberSessionScrollTop(currentSessionId, el);

    // 当滚动到顶部且有更多历史消息时，加载更多
    if (el.scrollTop <= LOAD_OLDER_THRESHOLD_PX && canRequestOlderHistory && historyOnLoadMore) {
      void historyOnLoadMore();
    }
  }, [activeSessionId, canRequestOlderHistory, historyOnLoadMore, rememberSessionScrollTop]);

  useEffect(() => {
    const el = scrollContainerRef.current;
    const content = el?.firstElementChild;
    if (!el || !content || typeof ResizeObserver === 'undefined') return;

    if (stickToBottomUntilStableRef.current && !userScrolledUpRef.current) {
      scrollToBottom(el);
    }

    const observer = new ResizeObserver(() => {
      if (historyLoadingMore || historyPrepending) return;
      if (!stickToBottomUntilStableRef.current || userScrolledUpRef.current) return;
      scrollToBottom(el);
      updateHistoryLayoutSnapshot(activeSessionId ?? '', el);
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [
    activeSessionId,
    historyLoadingMore,
    historyPrepending,
    updateHistoryLayoutSnapshot,
  ]);

  // 检测鼠标滚轮事件，即使没有滚动条也能触发加载更多
  const handleWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    // 只有向上滚动时才触发
    if (e.deltaY < 0) {
      stickToBottomUntilStableRef.current = false;
    }
    if (e.deltaY < 0 && canRequestOlderHistory && historyOnLoadMore) {
      // 检查是否已经在顶部（没有滚动条时 scrollTop 始终为 0）
      const el = scrollContainerRef.current;
      if (el && el.scrollTop <= LOAD_OLDER_THRESHOLD_PX) {
        void historyOnLoadMore();
      }
    }
  }, [canRequestOlderHistory, historyOnLoadMore]);

  // 监听浏览器 tab 可见性变化：隐藏时记录位置，恢复可见时抑制自动滚底
  useEffect(() => {
    const handleVisibilityChange = () => {
      const el = scrollContainerRef.current;
      const currentSessionId = activeSessionId ?? '';
      if (document.hidden) {
        if (el) {
          rememberSessionScrollTop(currentSessionId, el);
        }
      } else {
        visibilityRestoredAtRef.current = Date.now();
        if (el) {
          restoreSessionScrollTop(currentSessionId, el);
        }
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, [activeSessionId, rememberSessionScrollTop, restoreSessionScrollTop]);

  useLayoutEffect(() => {
    const el = scrollContainerRef.current;
    if (!el) return;

    const snapshot = historyLayoutSnapshotRef.current;
    const currentSessionId = activeSessionId ?? '';

    if (
      lastSessionIdRef.current === currentSessionId &&
      hasHistoryPager &&
      snapshot &&
      snapshot.sessionId === currentSessionId &&
      snapshot.loadedPages > 0 &&
      historyLoadedPages > snapshot.loadedPages
    ) {
      const delta = el.scrollHeight - snapshot.scrollHeight;
      if (delta !== 0) {
        el.scrollTop = snapshot.scrollTop + delta;
        suppressNextScrollToEndRef.current = true;
      }
    }

    updateHistoryLayoutSnapshot(currentSessionId, el);
  }, [
    activeSessionId,
    hasHistoryPager,
    historyLoadedPages,
    messages.length,
    toolExecutionOrder.length,
    updateHistoryLayoutSnapshot,
  ]);

  useLayoutEffect(() => {
    const currentSessionId = activeSessionId ?? '';
    const el = scrollContainerRef.current;
    if (!el) return;

    // 切换 session 时恢复记忆位置；第一次访问则默认滚到底部
    if (lastSessionIdRef.current !== currentSessionId) {
      // 位置已经在 handleScroll / render 阶段记录，这里只恢复目标 session 的位置
      const restoredScrollTop = restoreSessionScrollTop(currentSessionId, el);
      if (!restoredScrollTop) {
        // 第一次访问该 session，从底部开始
        userScrolledUpRef.current = false;
        stickToBottomUntilStableRef.current = true;
        scrollToBottom(el);
        updateHistoryLayoutSnapshot(currentSessionId, el);
      }

      lastSessionIdRef.current = currentSessionId;
      return;
    }

    if (historyLoadingMore || historyPrepending) {
      return;
    }

    if (suppressNextScrollToEndRef.current) {
      suppressNextScrollToEndRef.current = false;
      return;
    }

    // tab 重新可见后 300ms 内不自动滚底，避免切回时被状态更新拉到底部
    if (Date.now() - visibilityRestoredAtRef.current < VISIBILITY_RESTORE_SCROLL_SUPPRESS_MS) {
      return;
    }

    // 只有当用户在底部时才自动滚动
    if (!userScrolledUpRef.current) {
      const el = scrollContainerRef.current;
      if (el) {
        stickToBottomUntilStableRef.current = true;
        scrollToBottom(el);
        updateHistoryLayoutSnapshot(activeSessionId ?? '', el);
      }
    }
  }, [
    activeSessionId,
    messages,
    isThinking,
    contextCompressionRuntime,
    contextCompressionSummary,
    historyLoadedPages,
    historyLoadingMore,
    historyPrepending,
    teamHumanShareCommands.length,
    updateHistoryLayoutSnapshot,
  ]);

  // 包装发送消息函数，添加滚动逻辑
  const handleSendMessage = useCallback((content: string, mediaItems?: MediaItem[]) => {
    setIsSending(true);
    onSendMessage(content, mediaItems);
  }, [onSendMessage]);

  // 当发送消息时强制滚动到底部
  useEffect(() => {
    if (isSending) {
      const el = scrollContainerRef.current;
      if (el) {
        scrollToBottom(el);
        updateHistoryLayoutSnapshot(activeSessionId ?? '', el);
      }
      userScrolledUpRef.current = false;
      stickToBottomUntilStableRef.current = true;
      setIsSending(false);
    }
  }, [activeSessionId, isSending, updateHistoryLayoutSnapshot]);

  const handleSuggestion = useCallback(
    (text: string) => handleSendMessage(text),
    [handleSendMessage],
  );

  const markDesktopFileDropZoneActive = useCallback(() => {
    desktopFileDropAcceptUntilRef.current = Date.now() + 1200;
  }, []);

  const clearDesktopFileDropZone = useCallback(() => {
    desktopFileDropAcceptUntilRef.current = 0;
  }, []);

  const canAcceptDesktopFileDrag = useCallback(() => {
    return isDesktopAttachmentDropEnabled || isDesktopShell();
  }, [isDesktopAttachmentDropEnabled]);

  const handleDesktopFileDragEnter = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!canAcceptDesktopFileDrag()) return;
      if (!Array.from(event.dataTransfer.types).includes('Files')) return;
      event.preventDefault();
      // OS file drags require copy; move/none show the forbidden cursor in WebView2.
      event.dataTransfer.dropEffect = 'copy';
      markDesktopFileDropZoneActive();
    },
    [canAcceptDesktopFileDrag, markDesktopFileDropZoneActive],
  );

  const handleDesktopFileDragOver = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!canAcceptDesktopFileDrag()) return;
      if (!Array.from(event.dataTransfer.types).includes('Files')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      markDesktopFileDropZoneActive();
    },
    [canAcceptDesktopFileDrag, markDesktopFileDropZoneActive],
  );

  const ingestDesktopLocalFiles = useCallback(
    (detail: DesktopLocalFilesEventDetail | null | undefined, files: LocalFilePick[]) => {
      if (detail?.source && detail.source !== 'drop') return;
      if (!files.length) {
        clearDesktopFileDropZone();
        return;
      }

      const dropId = typeof detail?.dropId === 'string' ? detail.dropId : null;
      if (dropId && lastConsumedDesktopDropIdRef.current === dropId) {
        clearDesktopFileDropZone();
        return;
      }

      const acceptByTime = Date.now() <= desktopFileDropAcceptUntilRef.current;
      const clientX = detail?.clientX;
      const clientY = detail?.clientY;
      const hasCoords = typeof clientX === 'number' && typeof clientY === 'number';
      let inZone = false;
      if (hasCoords) {
        const hit = document.elementFromPoint(clientX, clientY);
        inZone = Boolean(
          hit?.closest('.chat-panel-shell') || hit?.closest('.chat-layout__surface'),
        );
      }
      // Native bridge trusted=true always accepts (coords from WebView2 are often wrong).
      const trusted = detail?.trusted === true;
      if (!trusted && !acceptByTime && !inZone) {
        clearDesktopFileDropZone();
        return;
      }

      if (dropId) lastConsumedDesktopDropIdRef.current = dropId;
      inputAreaRef.current?.appendLocalFilePicks(files);
      clearDesktopFileDropZone();
    },
    [clearDesktopFileDropZone],
  );

  const handleDesktopFileDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      if (!canAcceptDesktopFileDrag()) return;
      if (!Array.from(event.dataTransfer.types).includes('Files')) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
      markDesktopFileDropZoneActive();
      // Files arrive via the durable ingest bridge invoked by desktop_app run_js.
      // Do NOT call pywebview APIs here: a JS->Python call racing the Python-side
      // drop handler's run_js deadlocks the UI thread (window freezes).
    },
    [canAcceptDesktopFileDrag, markDesktopFileDropZoneActive],
  );

  useEffect(() => {
    // Durable bridge lives in localFilePicker; ChatPanel only registers a consumer.
    // Never delete window.__JIUWEN_INGEST_LOCAL_FILES__ — Python run_js requires it.
    const unregister = registerDesktopLocalFilesConsumer((detail, files) => {
      ingestDesktopLocalFiles(detail, files);
    });

    const onDesktopLocalFiles = (event: Event) => {
      const detail = (event as CustomEvent<DesktopLocalFilesEventDetail>).detail;
      ingestDesktopLocalFiles(detail, normalizePicks(detail?.files));
    };

    const onDesktopFileDrag = (event: Event) => {
      const active = Boolean((event as CustomEvent<{ active?: boolean }>).detail?.active);
      if (active) {
        markDesktopFileDropZoneActive();
      }
    };

    window.addEventListener(DESKTOP_LOCAL_FILES_EVENT, onDesktopLocalFiles as EventListener);
    window.addEventListener(DESKTOP_FILE_DRAG_EVENT, onDesktopFileDrag as EventListener);
    return () => {
      unregister();
      window.removeEventListener(DESKTOP_LOCAL_FILES_EVENT, onDesktopLocalFiles as EventListener);
      window.removeEventListener(DESKTOP_FILE_DRAG_EVENT, onDesktopFileDrag as EventListener);
    };
  }, [ingestDesktopLocalFiles, markDesktopFileDropZoneActive]);

  return (
    <div
      className="chat-panel-shell flex flex-col h-full"
      data-testid="chat-panel"
      onDragEnter={handleDesktopFileDragEnter}
      onDragOver={handleDesktopFileDragOver}
      onDrop={handleDesktopFileDrop}
    >
      {turnChangeNotice ? (
        <div className="code-turn-change-toast" role="status" aria-live="polite" data-testid="chat-panel-code-turn-change-toast">
          <CheckCircle2 size={17} aria-hidden="true" />
          <span>{turnChangeNotice}</span>
        </div>
      ) : null}
      {shouldShowChatHeader && (
        <div className="chat-panel-header" data-testid="chat-panel-header">
          <div className="chat-panel-header__meta" data-testid="chat-panel-header-meta">
            <div className="chat-panel-header__title" title={sessionTitle} data-testid="chat-panel-header-title">
              {sessionTitle}
            </div>
            {sessionProjectName && (
              <div className="chat-panel-header__project" title={sessionProjectName} data-testid="chat-panel-header-project">
                <span className="chat-config-icon chat-config-icon--folder" aria-hidden="true" />
                <span>{sessionProjectName}</span>
              </div>
            )}
          </div>
          <div className="chat-panel-header__actions" data-testid="chat-panel-header-actions">
            {shouldShowShareExport && (
              <button
                type="button"
                className={`icon-btn share-export-btn ${isExportingShare ? 'share-export-btn--loading' : ''}`}
                data-testid="chat-panel-share-export"
                data-variant={isExportingShare ? 'exporting' : 'ready'}
                title={shareExportTitle}
                aria-label={shareExportTitle}
                aria-busy={isExportingShare}
                disabled={!canExportShare || isExportingShare}
                onClick={() => {
                  void onExportShare?.();
                }}
              >
                {isExportingShare ? (
                  <>
                    <LoaderCircle className="share-export-btn__spinner" size={14} strokeWidth={2} />
                    <span className="share-export-btn__label" data-testid="chat-panel-share-export-loading-label">{t('share.generating')}</span>
                  </>
                ) : (
                  <Share2 size={14} strokeWidth={2} />
                )}
              </button>
            )}
            {shouldShowHumanShare && (
              <button
                type="button"
                className="chat-header-icon-btn"
                data-testid="chat-panel-human-share-trigger"
                onClick={() => setHumanShareOpen(true)}
                title={t('humanShare.title')}
              >
                <Sparkles size={16} strokeWidth={2} />
              </button>
            )}
            <button
              type="button"
              className={`chat-header-icon-btn ${!teamAreaExpanded ? 'chat-header-icon-btn--active' : ''}`}
              data-testid="chat-panel-header-chat-toggle"
              data-variant="collapse"
              onClick={() => onToggleTeamArea?.(false)}
            >
              <img src={chatIcon} alt="" className="chat-header-icon-btn__icon" />
            </button>
            <button
              type="button"
              className={`chat-header-icon-btn ${teamAreaExpanded ? 'chat-header-icon-btn--active' : ''}`}
              data-testid="chat-panel-header-expand-toggle"
              data-variant="expand"
              onClick={() => onToggleTeamArea?.(true)}
            >
              <img src={expandIcon} alt="" className="chat-header-icon-btn__icon" />
            </button>
          </div>
        </div>
      )}
      {hasHarnessProgress && (
        <div className="sticky top-0 z-10 px-3 pt-2 bg-bg/95 backdrop-blur-sm" data-testid="chat-panel-harness-progress-mount">
          <HarnessProgressBar />
        </div>
      )}
      {humanShareOpen && (
        <HumanSharePanel
          commands={teamHumanShareCommands}
          onClose={() => setHumanShareOpen(false)}
        />
      )}
      <div ref={scrollContainerRef} className="chat-scroll flex-1 overflow-y-auto" data-testid="chat-panel-scroll" onScroll={handleScroll} onWheel={handleWheel}>
        <div className={chatContentClassName} data-testid="chat-panel-content">
          {hasConversation ? (
            <>
              {showHistoryRetry && historyOnLoadMore && (
                <div className="flex justify-center pb-3">
                  <button
                    type="button"
                    className="btn !px-3 !py-1.5 text-xs"
                    data-testid="chat-panel-history-retry"
                    onClick={() => void historyOnLoadMore()}
                  >
                    {t('chat.historyLoadMore')}
                  </button>
                </div>
              )}
              {hasTimelineContent ? (
                <>
                  <MessageList messages={messages} renderAfterMessage={renderCodeChangesAfterMessage} />
                  {shouldShowHumanShare && (
                    <HumanShareCard
                      commands={teamHumanShareCommands}
                      onShare={() => setHumanShareOpen(true)}
                    />
                  )}
                  <SubtaskProgress />
                  {/* 内联审批卡片（演进审批 & 权限审批共用） */}
                  <InlineQuestionCard onSubmit={onUserAnswer} />
                  <ContextCompressionLines
                    runtime={contextCompressionRuntime}
                    summary={contextCompressionSummary}
                  />
                </>
              ) : isHistoryRestoring ? (
                <div className="flex h-32 items-center justify-center" role="status" aria-live="polite" data-testid="chat-panel-history-loading">
                  <div className="text-sm text-text-muted">
                    {t('chat.historyLoading')}
                  </div>
                </div>
              ) : null}
            </>
          ) : (
            <div className="chat-welcome" data-testid="chat-panel-welcome">
              <img className="chat-welcome__banner" src={welcomeBanner} alt={t('chat.welcomeLogoAlt')} data-testid="chat-panel-welcome-banner" />
              <h2 className="chat-welcome__heading" data-testid="chat-panel-welcome-heading"><WelcomeHeading /></h2>
              <div className="chat-welcome__composer" data-testid="chat-panel-welcome-composer">
                <ActiveTeamGroupEntry isProcessing={isProcessing} teamAreaExpanded={teamAreaExpanded} />
                <AgentActivityCard isProcessing={isProcessing} onSendTask={handleSendMessage} />
                <InterruptResultBubble />
                <InteractionSlot onSubmit={onUserAnswer} />
                <InputArea
                  ref={inputAreaRef}
                  onSubmit={handleSendMessage}
                  onPersistMedia={onPersistMedia}
                  onPersistDocuments={onPersistDocuments}
                  onInterrupt={onInterrupt}
                  onCancel={onCancel}
                  onSwitchMode={onSwitchMode}
                  isProcessing={isProcessing}
                  autoFocusKey={autoFocusKey}
                  onNavigateToSkills={onNavigateToSkills}
                  permissionsEnabled={permissionsEnabled}
                  onSavePermission={onSavePermission}
                  onSetGoal={onSetGoal}
                  onClearGoal={onClearGoal}
                />
              </div>
              <div className="chat-suggestions" data-testid="chat-panel-welcome-suggestions">
                {suggestions.map((text) => (
                  <SuggestionCard key={text} text={text} onClick={() => handleSuggestion(text)} />
                ))}
              </div>
            </div>
          )}
          <div />
        </div>
      </div>

      {hasConversation && (
        <div className="chat-compose" data-testid="chat-panel-compose">
          <ActiveTeamGroupEntry isProcessing={isProcessing} teamAreaExpanded={teamAreaExpanded} />
          <AgentActivityCard isProcessing={isProcessing} onSendTask={handleSendMessage} />
          <InterruptResultBubble />
          <InteractionSlot onSubmit={onUserAnswer} />
          {onSetGoal && onPauseGoal && onResumeGoal && onClearGoal && (
            <GoalBar
              onSetGoal={onSetGoal}
              onPauseGoal={onPauseGoal}
              onResumeGoal={onResumeGoal}
              onClearGoal={onClearGoal}
            />
          )}
          <InputArea
            ref={inputAreaRef}
            onSubmit={handleSendMessage}
            onPersistMedia={onPersistMedia}
            onPersistDocuments={onPersistDocuments}
            onInterrupt={onInterrupt}
            onCancel={onCancel}
            onSwitchMode={onSwitchMode}
            isProcessing={isProcessing}
            autoFocusKey={autoFocusKey}
            onNavigateToSkills={onNavigateToSkills}
            permissionsEnabled={permissionsEnabled}
            onSavePermission={onSavePermission}
            onSetGoal={onSetGoal}
            onClearGoal={onClearGoal}
            onDrainTaskQueueIfIdle={onDrainTaskQueueIfIdle}
          />
        </div>
      )}
      <div className="chat-ai-disclaimer" data-testid="chat-panel-ai-disclaimer">
        {t('share.aiNotice')}
      </div>
    </div>
  );
}
