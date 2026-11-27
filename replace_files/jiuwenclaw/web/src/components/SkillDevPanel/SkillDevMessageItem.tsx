/**
 * SkillDevMessageItem - SkillDev 消息项组件
 *
 * 按消息类型分流渲染：
 * - confirm_request / confirm_resolved: 审批卡片
 * - thinking_block: 可折叠思考块
 * - tool_call / tool_result: 工具调用展示
 * - 其他: 标准 Markdown 渲染
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { ToolCallDisplay } from '../ChatPanel/ToolCallDisplay';
import { StreamingContent } from '../ChatPanel/StreamingContent';
import { MediaRenderer } from '../ChatPanel/MediaRenderer';
import { SkillDevApprovalCard } from './SkillDevApprovalCard';
import { SkillDevThinkingBlock } from './SkillDevThinkingBlock';
import { SkillDevAgentRunCard } from './SkillDevAgentRunCard';
import type { SkillDevMessage } from '../../stores';
import type { SkillDevPlan, ClarifyAnswer } from '../../types/skilldev';
import { formatTimestamp, onTtsStop, sanitizeTtsText } from '../../utils';
import { useSpeechSynthesis } from '../../hooks';
import clsx from 'clsx';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface SkillDevMessageItemProps {
  message: SkillDevMessage;
  autoSpeak?: boolean;
  onConfirm?: (
    action: string,
    data?: {
      plan?: SkillDevPlan;
      feedback?: string;
      answers?: ClarifyAnswer[];
      messageId?: string;
    }
  ) => Promise<void> | void;
}

export function SkillDevMessageItem({ message, autoSpeak = false, onConfirm }: SkillDevMessageItemProps) {
  const { t } = useTranslation();
  const {
    role,
    content,
    type,
    timestamp,
    isStreaming,
    toolCall,
    toolResult,
    audioBase64,
    audioMime,
    mediaItems,
  } = message;
  const [hasAutoSpoken, setHasAutoSpoken] = useState(false);
  const [isAudioPlaying, setIsAudioPlaying] = useState(false);
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
      const cleanContent = sanitizeTtsText(content);
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
    try {
      await navigator.clipboard.writeText(content);
    } catch {
      const textarea = document.createElement('textarea');
      textarea.value = content;
      textarea.style.position = 'fixed';
      textarea.style.opacity = '0';
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand('copy');
      document.body.removeChild(textarea);
    }
  }, [content]);

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

  // 工具调用/结果消息
  if (role === 'tool' || type === 'tool_call' || type === 'tool_result') {
    return (
      <ToolCallDisplay
        toolCall={toolCall}
        toolResult={toolResult}
      />
    );
  }

  // 审批请求/已解决卡片
  if (type === 'confirm_request' || type === 'confirm_resolved') {
    return (
      <SkillDevApprovalCard
        message={message}
        onConfirm={onConfirm}
        disabled={type === 'confirm_resolved'}
      />
    );
  }

  // TEST_RUN 每个测试用例的独立执行卡片
  if (type === 'agent_run_card') {
    return <SkillDevAgentRunCard message={message} />;
  }

  // 可折叠思考块 或 流式思考（统一走 SkillDevThinkingBlock）
  if (type === 'thinking' || type === 'thinking_block') {
    return <SkillDevThinkingBlock message={message} />;
  }

  // 系统消息
  if (role === 'system') {
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
  const showTTS = Boolean(
    !isUser && !isStreaming && content && (ttsSupported || audioBase64)
  );
  const showCopy = Boolean(content) && !isStreaming;
  const isPlaying = audioBase64 ? isAudioPlaying : isSpeaking;

  return (
    <div className={clsx(
      'flex mb-3 animate-rise',
      isUser ? 'justify-end' : 'justify-start'
    )}>
      <div className="max-w-[82%] min-w-0">
        {!isUser && (
          <div className="hidden" data-testid="thinking-summary" aria-hidden="true" />
        )}

        <div
          className={clsx(
            'chat-bubble relative group',
            isUser ? 'user' : 'assistant',
            isStreaming && 'streaming'
          )}
          data-testid={!isUser ? 'thinking-panel' : undefined}
        >
          {isStreaming ? (
            <StreamingContent content={content} isStreaming={true} />
          ) : (
            <>
              <div className="chat-text" data-testid={!isUser ? 'thinking-body' : undefined}>
                {isUser ? (
                  <span className="whitespace-pre-wrap">{content}</span>
                ) : (
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      a: ({ node, href, children, ...props }) => (
                        <a
                          href={href}
                          target="_blank"
                          rel="noopener noreferrer"
                          {...props}
                        >
                          {children}
                        </a>
                      )
                    }}
                  >
                    {content}
                  </ReactMarkdown>
                )}
              </div>
              {mediaItems && mediaItems.length > 0 && (
                <MediaRenderer items={mediaItems} />
              )}
            </>
          )}
        </div>

        {/* 时间戳和操作 */}
        <div
          className={clsx(
            'flex items-center gap-3 text-sm mt-2 text-text-muted',
            isUser ? 'justify-end' : 'justify-start'
          )}
        >
          <span>{formatTimestamp(timestamp)}</span>

          {showCopy && (
            <button
              onClick={handleCopy}
              className="p-1.5 rounded-md transition-colors hover:text-accent hover:bg-secondary"
              title={t('chatUi.copyMessage')}
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 6.75h7.5m-7.5 3h7.5m-7.5 3h4.5M6.75 3h7.5A2.25 2.25 0 0116.5 5.25v13.5A2.25 2.25 0 0114.25 21h-7.5A2.25 2.25 0 014.5 18.75V5.25A2.25 2.25 0 016.75 3z" />
              </svg>
            </button>
          )}

          {showTTS && (
            <button
              onClick={handleSpeak}
              className={clsx(
                'p-1.5 rounded-md transition-colors',
                isPlaying
                  ? 'text-accent bg-accent/10'
                  : 'hover:text-accent hover:bg-secondary'
              )}
              title={isPlaying ? t('chatUi.stopReading') : t('chatUi.readMessage')}
            >
              {isPlaying ? (
                <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                  <rect x="6" y="6" width="12" height="12" rx="2" />
                </svg>
              ) : (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
                </svg>
              )}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
