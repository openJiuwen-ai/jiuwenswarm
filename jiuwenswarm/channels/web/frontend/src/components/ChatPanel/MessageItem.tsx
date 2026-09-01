/**
 * MessageItem 组件
 *
 * 单条消息显示，支持 TTS 朗读
 */

import { useState, useCallback, useEffect, useRef, memo } from 'react';
import type { ReactNode } from 'react';
import {
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  Info,
  MessageCircle,
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
  WebError,
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
import { onTtsStop, sanitizeTtsText } from '../../utils';
import { useSpeechSynthesis } from '../../hooks';
import clsx from 'clsx';
import { MarkdownRenderer } from '../../components/MarkdownRenderer';
import { isTeamP2PMessageToUser, parseTeamEventMessage } from './teamEventUtils';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { ProactiveRecommendationCard } from './ProactiveRecommendationCard';
import { fileArtifactId } from '../ArtifactsPanel';
import { openArtifactPanel } from '../../features/teamPanelState';
import { openSingleAgentPanel } from '../../features/singleAgentPanelState';
import { executeDesktopSave, type DesktopSaveApiResult } from '../../utils/desktopSave';
import { FileIcon } from '../FileIcon';
import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import { extractTokenFromDownloadUrl } from '../../utils/fileDownloadDedup';

function openArtifactPanelForActiveMode(selectedArtifactId: string): void {
  const sessionId = useChatStore.getState().activeSessionId;
  const mode = useSessionStore.getState().runtimes[sessionId ?? '']?.mode ?? 'agent';
  if (mode === 'team' || mode === 'auto_harness') {
    openArtifactPanel(selectedArtifactId);
    return;
  }
  openSingleAgentPanel('artifacts', selectedArtifactId);
}

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

function BtwCommandCard({
  command,
  output,
}: {
  command: string;
  output: string;
}) {
  const [expanded, setExpanded] = useState(true);
  const [answerCopied, setAnswerCopied] = useState(false);
  const question = command.replace(/^\/btw(?:\s+|$)/i, '').trim();

  const copyAnswer = useCallback(async () => {
    if (!output) return;
    try {
      await navigator.clipboard.writeText(output);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = output;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
    }
    setAnswerCopied(true);
    window.setTimeout(() => setAnswerCopied(false), 2000);
  }, [output]);

  return (
    <section className="chat-btw-card animate-fade-in" data-testid="chat-panel-btw-card">
      <button
        type="button"
        className="chat-btw-card__header"
        aria-expanded={expanded}
        data-testid="chat-panel-btw-card-toggle"
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="chat-btw-card__icon" aria-hidden="true">
          <MessageCircle size={16} strokeWidth={2} />
        </span>
        <span className="chat-btw-card__heading">
          <span className="chat-btw-card__badge">BTW</span>
          <span className="chat-btw-card__title">侧问</span>
        </span>
        <span className="chat-btw-card__scope">快速侧问，不打断主对话（基于当前上下文）</span>
        <span className="chat-btw-card__chevron" aria-hidden="true">
          {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
        </span>
      </button>

      {expanded && (
        <div className="chat-btw-card__body" data-testid="chat-panel-btw-card-body">
          {question && (
            <div className="chat-btw-card__question">
              <span className="chat-btw-card__section-label">问题</span>
              <span className="chat-btw-card__question-text">{question}</span>
            </div>
          )}
          <div className="chat-btw-card__answer">
            <div className="chat-btw-card__answer-header">
              <span className="chat-btw-card__section-label">回答</span>
              <button
                type="button"
                className="chat-btw-card__copy"
                onClick={() => void copyAnswer()}
                disabled={!output}
                data-testid="chat-panel-btw-card-copy"
              >
                {answerCopied ? <Check size={14} strokeWidth={2.2} /> : <Copy size={14} />}
                <span>{answerCopied ? '已复制' : '复制'}</span>
              </button>
            </div>
            {output ? (
              <MarkdownMessageBody
                content={output}
                className="chat-btw-card__answer-content"
                testId="chat-panel-btw-card-answer"
              />
            ) : (
              <span className="chat-btw-card__empty">暂无回答</span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function CompactCommandDivider({ output }: { output: string }) {
  return (
    <div
      className="chat-compact-divider animate-fade-in"
      data-testid="chat-panel-compact-divider"
    >
      <span className="chat-compact-divider__line" aria-hidden="true" />
      <span className="chat-compact-divider__label">{output}</span>
      <span className="chat-compact-divider__line" aria-hidden="true" />
    </div>
  );
}

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
    <div className="team-member-message animate-fade-in" data-testid="chat-panel-team-member-message">
      {/* 与单 agent 的 assistant-row 一致：无头像时整列不渲染，正文直接对齐最左边。 */}
      {showAvatar ? (
        <div className="team-member-message__header" data-testid="chat-panel-team-member-message-header">
          <TeamMemberAvatar member={member} />
        </div>
      ) : null}
      <div className={clsx('team-member-message__body', contentClassName)} data-testid="chat-panel-team-member-message-body">
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
          className="chat-message-file-list"
          onPreview={(index) => openArtifactPanelForActiveMode(fileArtifactId(fileItems[index]))}
        />
      )}
      <div className="team-member-message__plain" data-testid="chat-panel-team-leader-message-plain">
        <A2UIMessageContent
          content={content}
          messageId={messageId}
          isStreaming={isStreaming}
          disableInteraction={disableA2UIInteraction}
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
    <div className="context-compression-lines" data-testid="chat-panel-context-compression-lines">
      {showRuntime && (
        <div
          className={clsx(
            'mt-2 flex items-center gap-1.5 text-xs',
            isFailed ? 'text-danger' : 'text-text-muted'
          )}
          data-testid="chat-panel-context-compression-runtime"
          data-variant={isRunning ? 'running' : isFailed ? 'failed' : 'done'}
        >
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
          data-testid="chat-panel-context-compression-summary"
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
      <span
        key={`skill-${key++}`}
        className="chat-message-skill-chip"
        data-testid="chat-panel-message-skill-chip"
        data-variant={match[1]}
      >
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
    isStreaming,
    toolCall,
    toolResult,
    audioBase64,
    audioMime,
    mediaItems,
    fileItems,
    isGoalObjectiveMessage,
    isCommandOutput,
    commandName,
    commandInput,
    commandOutput,
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

  // 主动推荐消息 - 使用特殊卡片样式。外层沿用正常 agent 回复的布局（avatar 占位
  // + gap + chat-bubble-wrapper），使卡片左右边缘与回复正文气泡对齐，避免卡片
  // 左缘超出回复列。
  if (message.isProactiveRecommendation) {
    const withAssistantAvatar = enableAssistantAvatar;
    return (
      <div className={clsx('flex animate-rise justify-start', withAssistantAvatar && 'assistant-row')} data-testid="chat-panel-proactive-row">
        {withAssistantAvatar && (
          <div className="assistant-row__avatar" aria-hidden={!showAvatar} data-testid="chat-panel-proactive-avatar">
            {showAvatar ? <TeamMemberAvatar member="team_leader" /> : null}
          </div>
        )}
        <div className="chat-bubble-wrapper  min-w-0 flex-1" data-testid="chat-panel-proactive-bubble-wrapper">
          <ProactiveRecommendationCard message={message} />
        </div>
      </div>
    );
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
    // slash 命令输出按命令类型路由：BTW 使用侧问卡片，compact 使用时间线分隔条，
    // 其余命令退回通用文本；isCommandOutput 标记不会影响其他 system 消息。
    if (isCommandOutput) {
      const newlineIdx = content.indexOf('\n');
      const command = commandInput ?? (newlineIdx >= 0 ? content.slice(0, newlineIdx) : content);
      const output = commandOutput ?? (newlineIdx >= 0 ? content.slice(newlineIdx + 1).trim() : '');
      const normalizedCommandName = commandName || command.match(/^\/([\w-]+)/)?.[1]?.toLowerCase();

      if (normalizedCommandName === 'btw') {
        return <BtwCommandCard command={command} output={output} />;
      }

      if (normalizedCommandName === 'compact') {
        return <CompactCommandDivider output={output} />;
      }

      return (
        <div className="flex justify-center my-2 animate-fade-in">
          <div className="w-[85%] max-w-[44rem] px-2 py-0.5 text-xs leading-5 text-left text-text-muted">
            <span className="font-mono">{command}</span>
            {output && (
              <span className="mt-0.5 block whitespace-pre-wrap break-words">{output}</span>
            )}
          </div>
        </div>
      );
    }
 	     // 检查是否为 chat.session_result 事件
 	     if (content && content.startsWith('chat.session_result:')) {
 	       console.log('chat.session_result event:', content);
 	       const [, jsonStr] = content.split('chat.session_result:');
 	       try {
 	         const sessionData = JSON.parse(jsonStr);
 	         console.log('Parsed session data:', sessionData);
 	         const { description, result } = sessionData;
 	         
 	         return (
 	           <div className="chat-tool-card animate-rise" data-testid="chat-panel-session-result-card">
 	             <div
 	               className="cursor-pointer"
 	               onClick={() => setIsExpanded(!isExpanded)}
	               data-testid="chat-panel-session-result-card-header"
 	             >
 	               <div className="flex items-center gap-2">
 	                 <span className="w-5 h-5 rounded bg-accent-2-subtle text-accent-2 flex items-center justify-center text-sm">
 	                   <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
 	                     <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h3.75M9 15h3.75M9 18h3.75m3 .75H18a2.25 2.25 0 002.25-2.25V6.108c0-1.135-.845-2.098-1.976-2.192a48.424 48.424 0 00-1.123-.08m-5.801 0c-.065.21-.1.433-.1.664 0 .414.336.75.75.75h4.5a.75.75 0 00.75-.75 2.25 2.25 0 00-.1-.664m-5.8 0A2.251 2.251 0 0113.5 2.25H15c1.012 0 1.867.668 2.15 1.586m-5.8 0c-.376.023-.75.05-1.124.08C9.095 4.01 8.25 4.973 8.25 6.108V19.5a2.25 2.25 0 002.25 2.25h.75m0-3h-3.75m0 0h-3.75m0 0H9m1.5 3h3.75m-3.75 0H9m1.5 3h3.75m-3.75 0H9m1.5 3h3.75m-3.75 0H9" />
 	                   </svg>
 	                 </span>
 	                 <span className="font-mono text-sm font-medium text-text" data-testid="chat-panel-session-result-card-title">
 	                   会话任务：【{description || '未知任务'}】已完成
 	                 </span>
 	                 <span className="text-text-muted text-sm">
 	                   {isExpanded ? '▼' : '▶'}
 	                 </span>
 	               </div>
 	             </div>
 	             {isExpanded && (
 	               <div className="mt-2 p-2 rounded-md bg-card border border-border" data-testid="chat-panel-session-result-card-details">
 	                 {description && (
 	                   <div className="mb-2" data-testid="chat-panel-session-result-card-description">
 	                     <div className="font-mono text-xs text-text-muted mb-1">Description:</div>
 	                     <pre className="font-mono text-sm text-text overflow-x-auto whitespace-pre-wrap">
 	                       {description}
 	                     </pre>
 	                   </div>
 	                 )}
 	                 {result && (
 	                   <div data-testid="chat-panel-session-result-card-result">
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
 	           <div className="flex justify-center my-4 animate-fade-in" data-testid="chat-panel-session-result-fallback">
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
	               <div className="team-member-message__card" data-testid="chat-panel-team-event-card">
	                 <div className="team-member-message__content" data-testid="chat-panel-team-event-card-content">
	                   {event.isP2P && event.toMember && (
	                     <span className="team-event-group-chip team-event-group-chip--p2p" data-testid="chat-panel-team-event-chip-p2p">
	                       @{event.toMember}
	                     </span>
	                   )}
	                   {event.isBroadcast && (
	                     <span className="team-event-group-chip team-event-group-chip--broadcast" data-testid="chat-panel-team-event-chip-broadcast">
	                       {t('chat.teamBroadcastTarget')}
	                     </span>
	                   )}
	                   <MarkdownMessageBody
	                     content={event.content}
	                     className="team-message-markdown team-message-markdown--inline" data-testid="chat-panel-team-event-card-body"
	                   />
	                 </div>
	               </div>
	             </TeamMemberMessageFrame>
	           );
	       }
	       return (
	         <div className="flex justify-center my-4 animate-fade-in" data-testid="chat-panel-team-event-fallback">
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
      <div className="flex justify-center my-4 animate-fade-in" data-testid="chat-panel-system-message-bubble">
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
    <div
    data-testid="chat-panel-message-row"
    className={clsx(
      'flex animate-rise',
      isUser ? 'justify-end' : 'justify-start',
      withAssistantAvatar && 'assistant-row',
      withAssistantAvatar && !showAvatar && 'assistant-row--no-avatar'
    )}>
      {withAssistantAvatar && showAvatar ? (
        <div className="assistant-row__avatar" data-testid="chat-panel-assistant-row-avatar">
          <TeamMemberAvatar member="team_leader" />
        </div>
      ) : null}
      <div
        className={clsx(
          'chat-bubble-wrapper  min-w-0',
          !isUser && visibleFileItems && 'chat-bubble-wrapper--with-files'
        )}
        data-testid="chat-panel-bubble-wrapper"
      >
        {!isUser && (
          <div className="hidden" data-testid="chat-panel-thinking-summary" aria-hidden="true" />
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
            data-testid="chat-panel-message-bubble"
            data-variant={isUser ? 'user' : 'assistant'}
            data-state={isStreaming ? 'streaming' : 'final'}
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
                />
              )
            ) : (
              <>
                {isUser ? (
                  hasDisplayText ? (
                    <div className="chat-text" data-testid="chat-panel-message-text">
                      <span className="whitespace-pre-wrap">{renderRichContent(displayContent)}</span>
                    </div>
                  ) : null
                ) : (
                  <A2UIMessageContent
                    key={`${id}-final`}
                    content={content}
                    messageId={id}
                    disableInteraction={disableA2UIInteraction}
                  />
                )}
                {!isUser && visibleMediaItems && (
                  <MediaRenderer items={visibleMediaItems} align="start" />
                )}
                {visibleFileItems && (
                  <FileDownloadList
                    files={visibleFileItems}
                    className="chat-message-file-list"
                    onPreview={(index) => openArtifactPanelForActiveMode(fileArtifactId(visibleFileItems[index]))}
                  />
                )}
              </>
            )}
          </div>
        )}

        {/* Token usage summary */}
        {!isUser && !isStreaming && message.usageSummary && message.usageSummary.total_tokens > 0 && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-text-muted mt-1 mb-0.5" data-testid="chat-panel-message-usage-summary">
            <span>
              {message.usageSummary.input_tokens.toLocaleString()} in /{' '}
              {message.usageSummary.output_tokens.toLocaleString()} out /{' '}
              {message.usageSummary.total_tokens.toLocaleString()} total
            </span>
            {message.usageSummary.total_cost != null && message.usageSummary.total_cost > 0 && (
              <span>
                ${message.usageSummary.input_cost?.toFixed(4)} in /{' '}
                ${message.usageSummary.output_cost?.toFixed(4)} out /{' '}
                ${message.usageSummary.total_cost.toFixed(4)} total
              </span>
            )}
          </div>
        )}

        {!isStreaming && !hideMeta && (
          <div
            data-testid="chat-panel-message-meta"
            className={clsx(
              'flex items-center gap-1 text-sm mt-2 text-text-muted',
              isUser ? 'justify-end' : 'justify-start'
            )}
          >
            {isUser && isGoalObjectiveMessage && (
              <span className="inline-flex items-center gap-1 rounded-full bg-secondary px-2 py-0.5 text-xs text-text-muted" data-testid="chat-panel-message-goal-badge">
                <Target className="w-3 h-3" strokeWidth={2} />
                {t('goal.badge')}
              </span>
            )}

            {showCopy && (
              <div className="relative" data-testid="chat-panel-message-copy">
                {copied && (
                  <span className="animate-fade-in absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-xs text-text shadow-md" data-testid="chat-panel-message-copied-tip">
                    {t('chatUi.copied')}
                  </span>
                )}
                <button
                  data-testid="chat-panel-message-copy-btn"
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
                data-testid="chat-panel-message-tts-btn"
                data-variant={isPlaying ? 'playing' : 'idle'}
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

/** 识别可保存的 Skill 包：`.skill` / `.zip` / `.skill.zip` */
function isSkillPackageFile(file: FileDownloadItem): boolean {
  const candidates = [file.name, file.path].filter(Boolean) as string[];
  for (const candidate of candidates) {
    const base = candidate.replace(/\\/g, '/').split('/').pop()?.toLowerCase() || '';
    if (
      base.endsWith('.skill.zip') ||
      base.endsWith('.skill') ||
      base.endsWith('.zip')
    ) {
      return true;
    }
  }
  return false;
}

function skillPackageDisplayName(file: FileDownloadItem): string {
  const raw = (file.name || file.path || '').replace(/\\/g, '/').split('/').pop() || '';
  return raw.replace(/(\.skill)?\.zip$/i, '').replace(/\.skill$/i, '') || raw || 'skill';
}

function resolveFileDownloadToken(file: FileDownloadItem): string | undefined {
  const direct = file.download_token?.trim();
  if (direct) return direct;
  return extractTokenFromDownloadUrl(file.download_url)?.trim() || undefined;
}

function isImportOverwriteRequired(error: unknown): boolean {
  const code = (error as WebError | undefined)?.code;
  if (code === 'SKILL_IMPORT_OVERWRITE_REQUIRED' || code === 'SKILL_ALREADY_EXISTS') {
    return true;
  }
  const msg = error instanceof Error ? error.message : String(error);
  return msg.includes('已存在') || msg.includes('force=true') || msg.includes('IMPORT_OVERWRITE');
}

const SAVED_SKILLS_KEY = 'saved_skill_tokens';

function persistSavedToken(token: string) {
  try {
    const raw = localStorage.getItem(SAVED_SKILLS_KEY);
    const set = raw ? new Set<string>(JSON.parse(raw)) : new Set<string>();
    set.add(token);
    localStorage.setItem(SAVED_SKILLS_KEY, JSON.stringify([...set]));
  } catch { /* ignore */ }
}

function getSavedSkillTokens(): Set<string> {
  try {
    const raw = localStorage.getItem(SAVED_SKILLS_KEY);
    return raw ? new Set<string>(JSON.parse(raw)) : new Set<string>();
  } catch { return new Set<string>(); }
}

function getFileExtension(name: string): string {
  const parts = name.split('.');
  if (parts.length < 2) return '';
  return parts[parts.length - 1].toUpperCase();
}


function isInlineImageFile(mimeType: string | undefined, name: string): boolean {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  const mt = (mimeType || '').toLowerCase();
  return mt.startsWith('image/') || ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp'].includes(ext);
}

function isInlineVideoFile(mimeType: string | undefined, name: string): boolean {
  const ext = name.split('.').pop()?.toLowerCase() || '';
  const mt = (mimeType || '').toLowerCase();
  return mt.startsWith('video/') || ['mp4', 'avi', 'mov', 'mkv', 'webm', 'm4v'].includes(ext);
}

function inlineMediaUrl(downloadUrl: string): string {
  try {
    const url = new URL(downloadUrl, window.location.origin);
    url.searchParams.set('inline', '1');
    return url.pathname + url.search;
  } catch {
    const joiner = downloadUrl.includes('?') ? '&' : '?';
    return `${downloadUrl}${joiner}inline=1`;
  }
}

/** Prefer workspace path for durable inline preview; token URLs can fail after reload. */
function resolveInlineMediaSrc(file: FileDownloadItem): string | null {
  const path = file.path?.trim();
  if (path) {
    return `/file-api/raw-file?path=${encodeURIComponent(path)}`;
  }
  const downloadUrl = file.download_url?.trim();
  if (downloadUrl) {
    return inlineMediaUrl(downloadUrl);
  }
  return null;
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
  const [savingIndex, setSavingIndex] = useState<number | null>(null);
  const [savedIndex, setSavedIndex] = useState<Set<number>>(new Set());
  const [saveSuccessIndex, setSaveSuccessIndex] = useState<number | null>(null);
  const sessionId = useChatStore((s) => s.activeSessionId);

  useEffect(() => {
    let cancelled = false;
    files.forEach((file, index) => {
      // Path-backed files remain previewable via /file-api/raw-file even when
      // the signed download token is stale; only mark download as expired.
      const probeUrl = file.download_url?.trim();
      if (!probeUrl) {
        if (!file.path?.trim() && !cancelled) {
          setExpiredSet((prev) => new Set(prev).add(index));
        }
        return;
      }
      fetch(probeUrl, { method: 'HEAD' })
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

  // 挂载时从 localStorage 恢复已保存的技能索引
  useEffect(() => {
    const savedTokens = getSavedSkillTokens();
    if (savedTokens.size === 0) return;
    const restored = new Set<number>();
    files.forEach((file, index) => {
      const token = resolveFileDownloadToken(file);
      if (token && savedTokens.has(token)) restored.add(index);
    });
    if (restored.size > 0) setSavedIndex(restored);
  }, [files]);

  const handleDownload = async (file: FileDownloadItem, index: number) => {
    const path = file.path?.trim();
    const downloadUrl = file.download_url?.trim();
    const href = (!expiredSet.has(index) && downloadUrl)
      ? downloadUrl
      : (path ? `/file-api/raw-file?path=${encodeURIComponent(path)}` : '');
    if (!href) return;

    const pywebviewApi = (window as Window & { pywebview?: { api?: { download_file?: (url: string, filename: string) => DesktopSaveApiResult } } }).pywebview?.api;
    if (pywebviewApi?.download_file) {
      const outcome = await executeDesktopSave(() =>
        pywebviewApi.download_file!(href, file.name || 'download')
      );
      if (outcome === 'failed') {
        window.alert(t('artifacts.downloadFailed', { name: file.name }));
      }
      return;
    }
    const link = document.createElement('a');
    link.href = href;
    link.download = file.name || '';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleSaveSkill = async (file: FileDownloadItem, index: number) => {
    if (expiredSet.has(index) || savingIndex !== null || savedIndex.has(index)) return;
    const downloadToken = resolveFileDownloadToken(file);
    if (!downloadToken) return;
    if (!sessionId) {
      window.alert('当前无活跃会话，无法保存 Skill');
      return;
    }

    const importParams = (force: boolean) => ({
      download_token: downloadToken,
      force,
      session_id: sessionId,
    });

    setSavingIndex(index);
    try {
      await webRequest('skills.import_local', importParams(false));
      persistSavedToken(downloadToken);
      setSavedIndex((prev) => new Set(prev).add(index));
      setSaveSuccessIndex(index);
      setTimeout(() => setSaveSuccessIndex(null), 2000);
    } catch (error) {
      if (isImportOverwriteRequired(error)) {
        const errorMsg = error instanceof Error ? error.message : String(error);
        const overwrite = window.confirm(`${errorMsg}\n是否覆盖保存？`);
        if (!overwrite) return;
        try {
          await webRequest('skills.import_local', importParams(true));
          persistSavedToken(downloadToken);
          setSavedIndex((prev) => new Set(prev).add(index));
          setSaveSuccessIndex(index);
          setTimeout(() => setSaveSuccessIndex(null), 2000);
        } catch (err2) {
          console.error('skills.import_local force error:', err2);
          window.alert(err2 instanceof Error ? err2.message : String(err2));
        }
      } else {
        console.error('skills.import_local error:', error);
        window.alert(error instanceof Error ? error.message : String(error));
      }
    } finally {
      setSavingIndex(null);
    }
  };

  return (
    <div data-testid="chat-panel-file-download-list"
    className={clsx('mt-2 space-y-2', className)}>
      {files.map((file, index) => {
        const ext = getFileExtension(file.name);
        const hasPath = Boolean(file.path?.trim());
        // Fully unusable only when token probe failed and there is no path fallback.
        const expired = expiredSet.has(index) && !hasPath;
        const downloadExpired = expiredSet.has(index) && !hasPath;
        const mediaSrc = resolveInlineMediaSrc(file);
        const showImage = Boolean(mediaSrc) && isInlineImageFile(file.mime_type, file.name);
        const showVideo = Boolean(mediaSrc) && isInlineVideoFile(file.mime_type, file.name);
        const isSkill = isSkillPackageFile(file);
        const displayName = isSkill ? skillPackageDisplayName(file) : file.name;
        const downloadToken = resolveFileDownloadToken(file);
        const isSaving = savingIndex === index;
        const isSaved = savedIndex.has(index);
        return (
          <div key={`${file.name}-${index}`} className="space-y-2">
            {showImage && mediaSrc && (
              <img
                src={mediaSrc}
                alt={file.name}
                className="chat-msg-media-image max-h-80 w-auto cursor-pointer"
                data-testid="chat-inline-image"
                onClick={() => onPreview?.(index)}
              />
            )}
            {showVideo && mediaSrc && (
              <video
                controls
                preload="metadata"
                className="chat-msg-media-image max-h-80 w-full"
                data-testid="chat-inline-video"
              >
                <source src={mediaSrc} type={file.mime_type || 'video/mp4'} />
              </video>
            )}
          <div
            data-testid="chat-panel-file-download-item"
            data-variant={file.name}
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
              data-testid="chat-panel-file-download-preview"
              className="flex min-w-0 flex-1 items-center gap-3 text-left"
              disabled={expired || !onPreview}
              onClick={(event) => {
                event.stopPropagation();
                onPreview?.(index);
              }}
              title={onPreview ? t('artifacts.openPreview', { name: displayName }) : undefined}
              aria-label={onPreview ? t('artifacts.openPreview', { name: displayName }) : undefined}
            >
              {isSkill ? (
                <div className="flex-shrink-0 w-10 h-10 rounded-lg bg-accent-subtle flex items-center justify-center">
                  <svg className="w-5 h-5 text-accent" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
                  </svg>
                </div>
              ) : (
                <FileIcon fileName={file.name} size={40} className="flex-shrink-0 select-none" />
              )}
              <div className="flex-1 min-w-0" data-testid="chat-panel-file-download-info">
                <div className="text-sm font-medium text-text leading-snug truncate" data-testid="chat-panel-file-download-name">{displayName}</div>
                <div className="flex items-center gap-1.5 mt-0.5" data-testid="chat-panel-file-download-meta">
                  {!isSkill && (
                    <span className="inline-flex items-center px-1 py-px rounded text-[10px] font-mono font-medium text-text-muted bg-secondary leading-none" data-testid="chat-panel-file-download-ext">
                      {ext || 'FILE'}
                    </span>
                  )}
                  <span className="text-xs text-text-muted" data-testid="chat-panel-file-download-size">{formatFileSize(file.size)}</span>
                  {downloadExpired && (
                    <span className="inline-flex items-center px-1 py-px rounded text-[10px] font-mono font-medium text-danger bg-danger/10 leading-none" data-testid="chat-panel-file-download-expired">
                      {t('chatUi.fileExpired')}
                    </span>
                  )}
                </div>
              </div>
            </button>
            {isSkill ? (
              <div className="flex-shrink-0 flex items-center gap-2">
                {saveSuccessIndex === index && (
                  <span className="text-xs font-medium text-green-600 whitespace-nowrap">保存成功</span>
                )}
                <button
                  type="button"
                  className={clsx(
                    'flex-shrink-0 px-3 h-8 rounded-lg flex items-center justify-center text-sm font-medium transition-colors',
                    expired || isSaved
                      ? 'text-text-muted/40 cursor-not-allowed'
                      : isSaving
                        ? 'text-text-muted cursor-wait'
                        : 'text-accent hover:bg-accent-subtle'
                  )}
                  disabled={expired || isSaving || isSaved || !downloadToken}
                  onClick={(event) => {
                    event.stopPropagation();
                    void handleSaveSkill(file, index);
                  }}
                  title={isSaved ? '已保存' : '保存'}
                >
                  {isSaving ? '保存中...' : isSaved ? '已保存' : '保存'}
                </button>
              </div>
            ) : (
              <button
                type="button"
                data-testid="chat-panel-file-download-btn"
                className={clsx(
                  'flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center  ',
                  (!file.path?.trim() && downloadExpired)
                    ? 'text-text-muted/40'
                    : 'text-text-muted hover:text-accent hover:bg-accent-subtle'
                )}
                disabled={!file.path?.trim() && downloadExpired}
                onClick={(event) => {
                  event.stopPropagation();
                  void handleDownload(file, index);
                }}
                title={t('artifacts.download')}
                aria-label={t('artifacts.download')}
              >
                {(!file.path?.trim() && downloadExpired) ? (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                  </svg>
                ) : (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
                  </svg>
                )}
              </button>
            )}
          </div>
          </div>
        );
      })}
    </div>
  );
}
