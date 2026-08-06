/**
 * MessageItem 组件
 *
 * 单条消息显示，支持 TTS 朗读
 */

import { useState, useCallback, useEffect, useRef, memo } from 'react';
import type { ReactNode } from 'react';
import {
  Check,
  Copy,
  Info,
  Square,
  Target,
  Volume2,
} from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { contextCompressionRunningText } from '../../utils/contextCompression';
import {
  Message,
  FileDownloadItem,
  ContextCompressionRuntime,
  ContextCompressionSummary,
} from '../../types';
import { StreamingContent } from './StreamingContent';
import { ToolCallDisplay } from './ToolCallDisplay';
import { MediaRenderer, stripUploadDocumentBlocks } from './MediaRenderer';
import { A2UIMessageContent } from '../../features/a2ui/A2UIMessageContent';
import { QaSummaryCard } from '../InteractionSlot/QaSummaryCard';
import { isQaSummaryContent } from '../InteractionSlot/qaSummary';
import { GoalCompletedCard } from '../GoalBar/GoalCompletedCard';
import { isGoalCompletedContent } from '../GoalBar/goalCompletedMessage';
import { a2uiContentToText } from '../../features/a2ui/a2uiContent';
import { formatTimestamp, onTtsStop, sanitizeTtsText } from '../../utils';
import { useSpeechSynthesis } from '../../hooks';
import clsx from 'clsx';
import { MarkdownRenderer } from '../../components/MarkdownRenderer';
import { isTeamP2PMessageToUser, parseTeamEventMessage } from './teamEventUtils';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { ProactiveRecommendationCard } from './ProactiveRecommendationCard';
import { fileArtifactId } from '../ArtifactsPanel';
import { openArtifactPanel } from '../../features/teamPanelState';
import { executeDesktopSave, type DesktopSaveApiResult } from '../../utils/desktopSave';

export const MarkdownMessageBody = memo(function MarkdownMessageBody({
  content,
  className,
  testId,
}: {
  content: string;
  className?: string;
  testId?: string;
}) {
  return (
    <MarkdownRenderer
      content={content}
      className={clsx('chat-text chat-markdown', className)}
      testId={testId}
    />
  );
});

export function TeamMemberMessageFrame({
  member,
  showAvatar = true,
  children,
  contentClassName,
}: {
  member?: string;
  showAvatar?: boolean;
  children: ReactNode;
  contentClassName?: string;
}) {
  return (
    <div className="team-member-message animate-fade-in">
      {/* 始终占住头像列，避免 showAvatar 在多轮/折叠间切换时整列塌掉看起来像「头像消失」。 */}
      <div className="team-member-message__header" aria-hidden={!showAvatar}>
        {showAvatar ? <TeamMemberAvatar member={member} /> : null}
      </div>
      <div className={clsx('team-member-message__body', contentClassName)}>
        {children}
      </div>
    </div>
  );
}

function TeamLeaderPlainTextMessage({
  member = 'team_leader',
  content,
  messageId,
  isStreaming = false,
  showAvatar = true,
  fileItems,
  disableA2UIInteraction = false,
}: {
  member?: string;
  content: string;
  messageId: string;
  isStreaming?: boolean;
  showAvatar?: boolean;
  fileItems?: FileDownloadItem[];
  disableA2UIInteraction?: boolean;
}) {
  return (
    <TeamMemberMessageFrame
      member={member}
      showAvatar={showAvatar}
    >
      {fileItems && fileItems.length > 0 && (
        <FileDownloadList
          files={fileItems}
          className="w-full md:w-1/2"
          onPreview={(index) => openArtifactPanel(fileArtifactId(fileItems[index]))}
        />
      )}
      <div className="team-member-message__plain">
        <A2UIMessageContent
          content={content}
          messageId={messageId}
          isStreaming={isStreaming}
          disableInteraction={disableA2UIInteraction}
          testId="team-leader-message-body"
        />
      </div>
    </TeamMemberMessageFrame>
  );
}

export function ContextCompressionLines({
  runtime,
  summary,
  showSummary = true,
}: {
  runtime?: ContextCompressionRuntime;
  summary?: ContextCompressionSummary;
  showSummary?: boolean;
}) {
  const { t } = useTranslation();
  const showRuntime = Boolean(runtime?.summary);
  const finalSummary = !runtime && showSummary && summary && summary.count > 0 ? summary : null;
  if (!showRuntime && !finalSummary) return null;

  const isRunning = runtime?.status === 'running';
  const isFailed = runtime?.status === 'failed';
  const summaryItems = (finalSummary?.summaries ?? []).filter(Boolean);
  const detailText = summaryItems
    .map((item, index) => `${index + 1}. ${item}`)
    .join('\n');

  return (
    <div className="context-compression-lines">
      {showRuntime && (
        <div className={clsx(
          'mt-2 flex items-center gap-1.5 text-xs',
          isFailed ? 'text-danger' : 'text-text-muted'
        )}>
          <span className={clsx(isRunning && 'context-compression-running-text')}>
            {isRunning
              ? contextCompressionRunningText(t, runtime?.processor, runtime?.summary ?? '')
              : runtime?.summary}
          </span>
        </div>
      )}
      {finalSummary && (
        <div
          className="mt-2 flex items-center gap-1.5 text-xs text-text-muted"
          title={detailText || undefined}
        >
          <Info className="h-3.5 w-3.5" strokeWidth={1.8} />
          <span>
            {t('chat.contextCompressionCompleted', { count: finalSummary.count })}
          </span>
        </div>
      )}
    </div>
  );
}

/** 解析 content 里的 {{skill:名称}} 标记，返回 chip 与文字交织的节点数组 */
function renderRichContent(content: string): ReactNode[] {
  const parts: ReactNode[] = [];
  const regex = /\{\{skill:([^}]+)\}\}/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = regex.exec(content)) !== null) {
    if (match.index > lastIndex) {
      parts.push(content.slice(lastIndex, match.index));
    }
    parts.push(
      <span key={`skill-${key++}`} className="chat-message-skill-chip">
        <span className="chat-message-skill-chip__icon" aria-hidden="true" />
        <span className="chat-message-skill-chip__label">{match[1]}</span>
      </span>
    );
    lastIndex = regex.lastIndex;
  }
  if (lastIndex < content.length) {
    parts.push(content.slice(lastIndex));
  }
  return parts;
}

export function getMessageActor(message: Message): string | null {
  // team-leader 气泡偶发会落成 assistant；按 id 识别，避免 team 聚类把头像判丢。
  if (message.id?.startsWith('team-leader-')) {
    return 'team_leader';
  }

  if (message.role !== 'system') {
    return null;
  }

  if (message.content?.startsWith('team.event:')) {
    const event = parseTeamEventMessage(message);
    return event?.fromMember || null;
  }

  return null;
}

interface MessageItemProps {
  message: Message;
  autoSpeak?: boolean;
  showAvatar?: boolean;
  disableA2UIInteraction?: boolean;
  hideMeta?: boolean;
  enableAssistantAvatar?: boolean;
}

export const MessageItem = memo(function MessageItem({
  message,
  autoSpeak = false,
  showAvatar = true,
  disableA2UIInteraction = false,
  hideMeta = false,
  enableAssistantAvatar = false,
}: MessageItemProps) {
  const { t } = useTranslation();
  const {
    id,
    role,
    content,
    timestamp,
    isStreaming,
    toolCall,
    toolResult,
    audioBase64,
    audioMime,
    mediaItems,
    fileItems,
    isGoalObjectiveMessage,
  } = message;
  const [hasAutoSpoken, setHasAutoSpoken] = useState(false);
  const [isAudioPlaying, setIsAudioPlaying] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // TTS
  const { isSpeaking, speak, stop, isSupported: ttsSupported } = useSpeechSynthesis({
    language: 'zh-CN',
    rate: 1.1,
  });

  // 朗读消息
  const stopGeneratedAudio = useCallback(() => {
    if (audioRef.current) {
      audioRef.current.pause();
      audioRef.current.currentTime = 0;
      audioRef.current = null;
    }
    setIsAudioPlaying(false);
  }, []);

  const playGeneratedAudio = useCallback(async () => {
    if (!audioBase64) {
      return false;
    }

    stopGeneratedAudio();
    const audio = new Audio(
      `data:${audioMime || 'audio/mpeg'};base64,${audioBase64}`
    );
    audioRef.current = audio;
    audio.onended = () => {
      setIsAudioPlaying(false);
    };
    audio.onerror = () => {
      setIsAudioPlaying(false);
    };

    try {
      await audio.play();
      setIsAudioPlaying(true);
      return true;
    } catch {
      setIsAudioPlaying(false);
      return false;
    }
  }, [audioBase64, audioMime, stopGeneratedAudio]);

  const handleSpeak = useCallback(() => {
    if (audioBase64) {
      if (isAudioPlaying) {
        stopGeneratedAudio();
        return;
      }
      void playGeneratedAudio();
      return;
    }

    if (isSpeaking) {
      stop();
    } else if (content) {
      const readableContent = a2uiContentToText(content) || content;
      const cleanContent = sanitizeTtsText(readableContent);
      if (cleanContent) {
        speak(cleanContent);
      }
    }
  }, [
    audioBase64,
    content,
    isAudioPlaying,
    isSpeaking,
    playGeneratedAudio,
    speak,
    stop,
    stopGeneratedAudio,
  ]);

  const handleCopy = useCallback(async () => {
    if (!content) return;
    const raw = role === 'user' ? stripUploadDocumentBlocks(content) : content;
    if (!raw) return;
    const copyContent = a2uiContentToText(raw) || raw;
    try {
      await navigator.clipboard.writeText(copyContent);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = copyContent;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }, [content, role]);

  // 自动朗读新消息（仅助手消息，由父组件通过 autoSpeak 控制）
  useEffect(() => {
    if (autoSpeak && role === 'assistant' && !isStreaming && !hasAutoSpoken && content) {
      handleSpeak();
      setHasAutoSpoken(true);
    }
  }, [autoSpeak, role, isStreaming, hasAutoSpoken, content, handleSpeak]);

  useEffect(() => {
    return () => {
      stopGeneratedAudio();
    };
  }, [stopGeneratedAudio]);

  useEffect(() => {
    return onTtsStop(() => {
      stopGeneratedAudio();
      stop();
    });
  }, [stopGeneratedAudio, stop]);

  // 主动推荐消息 - 使用特殊卡片样式
  if (message.isProactiveRecommendation) {
    return <ProactiveRecommendationCard message={message} />;
  }

  // 工具调用/结果消息
  if (role === 'tool') {
    return (
      <ToolCallDisplay
        toolCall={toolCall}
        toolResult={toolResult}
      />
    );
  }

  // 交互问答「问题澄清」回显卡（ask_user 确认后前端合成注入）
  if (isQaSummaryContent(content)) {
    return <QaSummaryCard content={content} />;
  }

  // 目标完成回显卡（目标实时跳变到 completed 时前端合成注入）
  if (isGoalCompletedContent(content)) {
    return <GoalCompletedCard content={content} />;
  }

  // 系统消息
  if (role === 'system') {
 	     // 检查是否为 chat.session_result 事件
 	     if (content && content.startsWith('chat.session_result:')) {
 	       console.log('chat.session_result event:', content);
 	       const [, jsonStr] = content.split('chat.session_result:');
 	       try {
 	         const sessionData = JSON.parse(jsonStr);
 	         console.log('Parsed session data:', sessionData);
 	         const { description, result } = sessionData;
 	         
 	         return (
 	           <div className="chat-tool-card animate-rise">
 	             <div
 	               className="cursor-pointer"
 	               onClick={() => setIsExpanded(!isExpanded)}
 	             >
 	               <div className="flex items-center gap-2">
 	                 <span className="w-5 h-5 rounded bg-accent-2-subtle text-accent-2 flex items-center justify-center text-sm">
 	                   <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
 	                     <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V19.5a2.25 2.25 0 002.25 2.25h.75m0-3h-3.75m0 0h-3.75m0 0H9m1.5 3h3.75m-3.75 0H9m1.5 3h3.75m-3.75 0H9m1.5 3h3.75m-3.75 0H9" />
 	                   </svg>
 	                 </span>
 	                 <span className="font-mono text-sm font-medium text-text">
 	                   会话任务：【{description || '未知任务'}】已完成
 	                 </span>
 	                 <span className="text-text-muted text-sm">
 	                   {isExpanded ? '▼' : '▶'}
 	                 </span>
 	               </div>
 	             </div>
 	             {isExpanded && (
 	               <div className="mt-2 p-2 rounded-md bg-card border border-border">
 	                 {description && (
 	                   <div className="mb-2">
 	                     <div className="font-mono text-xs text-text-muted mb-1">Description:</div>
 	                     <pre className="font-mono text-sm text-text overflow-x-auto whitespace-pre-wrap">
 	                       {description}
 	                     </pre>
 	                   </div>
 	                 )}
 	                 {result && (
 	                   <div>
 	                     <div className="font-mono text-xs text-text-muted mb-1">Result:</div>
 	                     <pre className="font-mono text-sm text-text overflow-x-auto whitespace-pre-wrap max-h-60">
 	                       {result}
 	                     </pre>
 	                   </div>
 	                 )}
 	               </div>
 	             )}
 	           </div>
 	         );
 	       } catch (e) {
 	         // 如果解析失败，显示原始内容
 	         return (
 	           <div className="flex justify-center my-4 animate-fade-in">
 	             <div className="px-4 py-2 rounded-full bg-secondary border border-border text-text-muted text-sm">
 	               {content}
 	             </div>
 	           </div>
 	         );
 	       }
 	     }
	     
	     // 检查是否为团队消息
	     if (content && content.startsWith('team.event:')) {
	       const event = parseTeamEventMessage(message);
	       if (event) {
	           // 面向用户的团队消息直接展示在主会话
	           if (event.isLeaderToUser || isTeamP2PMessageToUser(event)) {
	             return (
	               <TeamLeaderPlainTextMessage
	                 member={event.fromMember}
	                 content={event.content}
	                 messageId={id}
	                 showAvatar={showAvatar}
	               />
	             );
	           }
	           
	           // p2p 和 broadcast 消息展示
	           return (
	             <TeamMemberMessageFrame
	               member={event.fromMember}
	               showAvatar={showAvatar}
	             >
	               <div className="team-member-message__card">
	                 <div className="team-member-message__content">
	                   {event.isP2P && event.toMember && (
	                     <span className="team-event-group-chip team-event-group-chip--p2p">
	                       @{event.toMember}
	                     </span>
	                   )}
	                   {event.isBroadcast && (
	                     <span className="team-event-group-chip team-event-group-chip--broadcast">
	                       {t('chat.teamBroadcastTarget')}
	                     </span>
	                   )}
	                   <MarkdownMessageBody
	                     content={event.content}
	                     className="team-message-markdown team-message-markdown--inline"
	                   />
	                 </div>
	               </div>
	             </TeamMemberMessageFrame>
	           );
	       }
	       return (
	         <div className="flex justify-center my-4 animate-fade-in">
	           <div className="px-4 py-2 rounded-full bg-secondary border border-border text-text-muted text-sm">
	             {content}
	           </div>
	         </div>
	       );
	     }
	     
	     // 检查是否为 team_leader 消息（通过 ID 判断）
	     const isTeamLeaderMsg = id && id.startsWith('team-leader-');
	     
	     if (isTeamLeaderMsg) {
	       let messageContent = content;
	       
	       if (content.startsWith('team.leader:')) {
	         const [, jsonStr] = content.split('team.leader:');
	         try {
	           const data = JSON.parse(jsonStr);
	           messageContent = data.content;
	         } catch (e) {
	         }
	       }
	       
	       return (
	         <TeamLeaderPlainTextMessage
	           member="team_leader"
	           content={messageContent || (isStreaming ? '正在接收中...' : '')}
	           messageId={id}
	           isStreaming={isStreaming}
	           showAvatar={showAvatar}
	           fileItems={fileItems}
	           disableA2UIInteraction={disableA2UIInteraction}
	         />
	       );
	     }
	     
    return (
      <div className="flex justify-center my-4 animate-fade-in">
        <div className="px-4 py-2 rounded-full bg-secondary border border-border text-text-muted text-sm">
          {content}
        </div>
      </div>
    );
  }

  // 用户/助手消息
  const isUser = role === 'user';
  const displayContent = isUser ? stripUploadDocumentBlocks(content) : content;
  const showTTS = Boolean(
    !isUser && !isStreaming && content && (ttsSupported || audioBase64)
  );
  const showCopy = Boolean(isUser ? displayContent : content) && !isStreaming;
  const isPlaying = audioBase64 ? isAudioPlaying : isSpeaking;
  const visibleMediaItems = mediaItems?.length ? mediaItems : null;
  const visibleFileItems = fileItems?.length ? fileItems : null;
  const hasDisplayText = Boolean(displayContent);
  const hasBubbleContent = isUser
    ? hasDisplayText || isStreaming
    : Boolean(content) || Boolean(visibleMediaItems) || Boolean(visibleFileItems);

  const withAssistantAvatar = !isUser && enableAssistantAvatar;

  return (
    <div className={clsx(
      'flex animate-rise',
      isUser ? 'justify-end' : 'justify-start',
      withAssistantAvatar && 'assistant-row'
    )}>
      {withAssistantAvatar && (
        // 始终保留头像占位，与 team 布局一致，避免连续气泡时整列消失。
        <div className="assistant-row__avatar" aria-hidden={!showAvatar}>
          {showAvatar ? <TeamMemberAvatar member="team_leader" /> : null}
        </div>
      )}
      <div className="chat-bubble-wrapper max-w-[82%] min-w-0">
        {!isUser && (
          <div className="hidden" data-testid="thinking-summary" aria-hidden="true" />
        )}

        {isUser && visibleMediaItems && (
          <MediaRenderer items={visibleMediaItems} align="end" variant="above" />
        )}

        {hasBubbleContent && (
          <div
            className={clsx(
              'chat-bubble relative group',
              isUser ? 'user' : 'assistant',
              !isUser && !isStreaming && 'markdown',
              isStreaming && 'streaming'
            )}
            data-testid={!isUser ? 'thinking-panel' : undefined}
          >
            {isStreaming ? (
              isUser ? (
                <StreamingContent content={displayContent} />
              ) : (
                <A2UIMessageContent
                  key={`${id}-streaming`}
                  content={content}
                  messageId={id}
                  isStreaming={true}
                  disableInteraction={disableA2UIInteraction}
                  testId="thinking-body"
                />
              )
            ) : (
              <>
                {isUser ? (
                  hasDisplayText ? (
                    <div className="chat-text">
                      <span className="whitespace-pre-wrap">{renderRichContent(displayContent)}</span>
                    </div>
                  ) : null
                ) : (
                  <A2UIMessageContent
                    key={`${id}-final`}
                    content={content}
                    messageId={id}
                    disableInteraction={disableA2UIInteraction}
                    testId="thinking-body"
                  />
                )}
                {!isUser && visibleMediaItems && (
                  <MediaRenderer items={visibleMediaItems} align="start" />
                )}
                {visibleFileItems && (
                  <FileDownloadList
                    files={visibleFileItems}
                    onPreview={(index) => openArtifactPanel(fileArtifactId(visibleFileItems[index]))}
                  />
                )}
              </>
            )}
          </div>
        )}

        {/* Token usage summary */}
        {!isUser && !isStreaming && message.usageSummary && message.usageSummary.total_tokens > 0 && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-text-muted mt-1 mb-0.5">
            <span>
              {message.usageSummary.input_tokens.toLocaleString()} in /{' '}
              {message.usageSummary.output_tokens.toLocaleString()} out /{' '}
              {message.usageSummary.total_tokens.toLocaleString()} total
            </span>
            {message.usageSummary.total_cost != null && message.usageSummary.total_cost > 0 && (
              <span>
                provider-reported{' '}
                {message.usageSummary.input_cost?.toFixed(4)} in /{' '}
                {message.usageSummary.output_cost?.toFixed(4)} out /{' '}
                {message.usageSummary.total_cost.toFixed(4)} total
                {message.usageSummary.currency ? ` ${message.usageSummary.currency}` : ''}
              </span>
            )}
          </div>
        )}

        {!isStreaming && !hideMeta && (
          <div
            className={clsx(
              'flex items-center gap-3 text-sm mt-2 text-text-muted',
              isUser ? 'justify-end' : 'justify-start'
            )}
          >
            <span>{formatTimestamp(timestamp)}</span>

            {isUser && isGoalObjectiveMessage && (
              <span className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs text-text-muted">
                <Target className="w-3 h-3" strokeWidth={2} />
                {t('goal.badge')}
              </span>
            )}

            {showCopy && (
              <div className="relative">
                {copied && (
                  <span className="animate-fade-in absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-xs text-text shadow-md">
                    {t('chatUi.copied')}
                  </span>
                )}
                <button
                  onClick={handleCopy}
                  className={clsx(
                    'p-1.5 rounded-md ',
                    copied ? 'text-accent' : 'hover:text-accent hover:bg-secondary'
                  )}
                  title={t('chatUi.copyMessage')}
                >
                  {copied ? (
                    <Check className="w-4 h-4" strokeWidth={1.5} />
                  ) : (
                    <Copy className="w-4 h-4" strokeWidth={1.5} />
                  )}
                </button>
              </div>
            )}

            {showTTS && (
              <button
                onClick={handleSpeak}
                className={clsx(
                  'p-1.5 rounded-md ',
                  isPlaying
                    ? 'text-accent bg-accent/10'
                    : 'hover:text-accent hover:bg-secondary'
                )}
                title={isPlaying ? t('chatUi.stopReading') : t('chatUi.readMessage')}
              >
                {isPlaying ? (
                  <Square className="w-4 h-4 fill-current" strokeWidth={1.5} />
                ) : (
                  <Volume2 className="w-4 h-4" strokeWidth={1.5} />
                )}
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
});

function formatFileSize(bytes: number | undefined): string {
  if (bytes === undefined || bytes === null || isNaN(bytes)) return '';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const size = bytes / Math.pow(1024, i);
  return `${size.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function getFileExtension(name: string): string {
  const parts = name.split('.');
  if (parts.length < 2) return '';
  return parts[parts.length - 1].toUpperCase();
}

function getFileTypeConfig(mimeType: string | undefined, name: string) {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  const mt = mimeType || '';
  if (mt.startsWith('image/') || ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp'].includes(ext))
    return { label: 'IMG', bg: 'bg-[#3370ff]', icon: '🖼' };
  if (mt.startsWith('audio/') || ['mp3', 'wav', 'aac', 'flac', 'ogg'].includes(ext))
    return { label: 'AUDIO', bg: 'bg-[#7b67ee]', icon: '🎵' };
  if (mt.startsWith('video/') || ['mp4', 'avi', 'mov', 'mkv', 'webm'].includes(ext))
    return { label: 'VIDEO', bg: 'bg-[#f77234]', icon: '🎬' };
  if (mt.includes('pdf') || ext === 'pdf')
    return { label: 'PDF', bg: 'bg-[#f54a45]', icon: '📄' };
  if (mt.includes('presentation') || mt.includes('ppt') || ['ppt', 'pptx'].includes(ext))
    return { label: 'PPT', bg: '#FFFFFF', icon: (
      <svg viewBox="0 0 1024 1024" className="w-7 h-7">
        <path d="M145.6 0C100.8 0 64 36.8 64 81.6v860.8C64 987.2 100.8 1024 145.6 1024h732.8c44.8 0 81.6-36.8 81.6-81.6V324.8L657.6 0h-512z" fill="#E34221" />
        <path d="M960 326.4v16H755.2s-100.8-20.8-99.2-108.8c0 0 4.8 92.8 97.6 92.8H960z" fill="#DC3119" />
        <path d="M657.6 0v233.6c0 25.6 17.6 92.8 97.6 92.8H960L657.6 0z" fill="#FFFFFF" opacity=".5" />
        <path d="M304 784h-54.4v67.2c0 6.4-4.8 11.2-11.2 11.2-6.4 0-12.8-4.8-12.8-11.2V686.4c0-9.6 8-17.6 17.6-17.6H304c38.4 0 59.2 25.6 59.2 57.6S340.8 784 304 784z m-3.2-94.4h-51.2v73.6h51.2c22.4 0 38.4-16 38.4-36.8 0-22.4-16-36.8-38.4-36.8zM480 784h-54.4v67.2c0 6.4-4.8 11.2-11.2 11.2-6.4 0-11.2-4.8-11.2-11.2V686.4c0-9.6 6.4-17.6 16-17.6H480c38.4 0 59.2 25.6 59.2 57.6S518.4 784 480 784z m-3.2-94.4h-49.6v73.6h49.6c22.4 0 38.4-16 38.4-36.8 0-22.4-16-36.8-38.4-36.8z m225.6 0h-52.8v161.6c0 6.4-4.8 11.2-11.2 11.2-6.4 0-12.8-4.8-12.8-11.2V689.6h-51.2c-6.4 0-11.2-4.8-11.2-11.2 0-4.8 4.8-9.6 11.2-9.6h128c6.4 0 11.2 4.8 11.2 11.2 0 4.8-4.8 9.6-11.2 9.6z" fill="#FFFFFF" />
      </svg>
    ) };
  if (mt.includes('spreadsheet') || mt.includes('excel') || mt.includes('xlsx') || ['xls', 'xlsx', 'csv'].includes(ext))
    return { label: 'XLS', bg: 'bg-[#2b9348]', icon: '📗' };
  if (mt.includes('word') || mt.includes('document') || mt.includes('docx') || ['doc', 'docx'].includes(ext))
    return { label: 'DOC', bg: 'bg-[#3370ff]', icon: '📝' };
  if (mt.includes('zip') || mt.includes('compressed') || mt.includes('archive') || ['zip', 'rar', '7z', 'tar', 'gz'].includes(ext))
    return { label: 'ZIP', bg: 'bg-[#8b5cf6]', icon: '📦' };
  if (['txt', 'md', 'log'].includes(ext))
    return { label: 'TXT', bg: 'bg-[#6b7280]', icon: '📃' };
  if (['json', 'xml', 'yaml', 'yml', 'toml', 'ini', 'cfg'].includes(ext))
    return { label: 'CFG', bg: 'bg-[#6b7280]', icon: '⚙' };
  if (['py', 'js', 'ts', 'java', 'go', 'rs', 'cpp', 'c', 'h'].includes(ext))
    return { label: 'CODE', bg: 'bg-[#6b7280]', icon: '�' };
  return { label: 'FILE', bg: 'bg-[#6b7280]', icon: '��' };
}

function FileDownloadList({
  files,
  className,
  onPreview,
}: {
  files: FileDownloadItem[];
  className?: string;
  onPreview?: (index: number) => void;
}) {
  const { t } = useTranslation();
  const [expiredSet, setExpiredSet] = useState<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    files.forEach((file, index) => {
      fetch(file.download_url, { method: 'HEAD' })
        .then((res) => {
          if (!cancelled && !res.ok) {
            setExpiredSet((prev) => new Set(prev).add(index));
          }
        })
        .catch(() => {
          if (!cancelled) {
            setExpiredSet((prev) => new Set(prev).add(index));
          }
        });
    });
    return () => { cancelled = true; };
  }, [files]);

  const handleDownload = async (file: FileDownloadItem, index: number) => {
    if (expiredSet.has(index)) return;

    // 检查是否在 PyWebView 桌面环境中
    const pywebviewApi = (window as Window & { pywebview?: { api?: { download_file?: (url: string, filename: string) => DesktopSaveApiResult } } }).pywebview?.api;
    if (pywebviewApi?.download_file) {
      // 桌面端：通过 webview API 下载
      const outcome = await executeDesktopSave(() =>
        pywebviewApi.download_file!(file.download_url, file.name || 'download')
      );
      if (outcome === 'failed') {
        window.alert(t('artifacts.downloadFailed', { name: file.name }));
      }
      return;
    }
    // 浏览器模式：使用标准 <a> 标签下载
    const link = document.createElement('a');
    link.href = file.download_url;
    link.download = file.name || '';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  return (
    <div className={clsx('mt-2 space-y-2', className)}>
      {files.map((file, index) => {
        const typeConfig = getFileTypeConfig(file.mime_type, file.name);
        const ext = getFileExtension(file.name);
        const expired = expiredSet.has(index);
        return (
          <div
            key={`${file.name}-${index}`}
            className={clsx(
              'flex items-center gap-3 rounded-lg border px-3 py-2.5  ',
              expired
                ? 'border-border/50 bg-card/50 cursor-not-allowed opacity-60'
                : clsx(
                  'border-border bg-card',
                  onPreview && 'cursor-pointer group hover:border-border-hover hover:shadow-md'
                )
            )}
            onClick={() => {
              if (!expired) onPreview?.(index);
            }}
          >
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-3 text-left"
              disabled={expired || !onPreview}
              onClick={(event) => {
                event.stopPropagation();
                onPreview?.(index);
              }}
              title={onPreview ? t('artifacts.openPreview', { name: file.name }) : undefined}
              aria-label={onPreview ? t('artifacts.openPreview', { name: file.name }) : undefined}
            >
              <div className={`flex-shrink-0 w-10 h-10 rounded-lg ${typeConfig.bg} flex items-center justify-center`}>
                {typeof typeConfig.icon === 'string' ? (
                  <span className="text-white text-base leading-none select-none">{typeConfig.icon}</span>
                ) : (
                  typeConfig.icon
                )}
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-sm font-medium text-text leading-snug truncate">{file.name}</div>
                <div className="flex items-center gap-1.5 mt-0.5">
                  <span className="inline-flex items-center px-1 py-px rounded text-[10px] font-mono font-medium text-text-muted bg-secondary leading-none">
                    {ext || typeConfig.label}
                  </span>
                  <span className="text-xs text-text-muted">{formatFileSize(file.size)}</span>
                  {expired && (
                    <span className="inline-flex items-center px-1 py-px rounded text-[10px] font-mono font-medium text-danger bg-danger/10 leading-none">
                      {t('chatUi.fileExpired')}
                    </span>
                  )}
                </div>
              </div>
            </button>
            <button
              type="button"
              className={clsx(
                'flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center  ',
                expired
                  ? 'text-text-muted/40'
                  : 'text-text-muted hover:text-accent hover:bg-accent-subtle'
              )}
              disabled={expired}
              onClick={(event) => {
                event.stopPropagation();
                void handleDownload(file, index);
              }}
              title={t('artifacts.download')}
              aria-label={t('artifacts.download')}
            >
              {expired ? (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                </svg>
              ) : (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
                </svg>
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}
