/**
 * MessageItem 组件
 *
 * 单条消息显示，支持 TTS 朗读
 */

import { useState, useCallback, useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Message } from '../../types';
import { StreamingContent } from './StreamingContent';
import { ToolCallDisplay } from './ToolCallDisplay';
import { MediaRenderer } from './MediaRenderer';
import { formatTimestamp, onTtsStop, sanitizeTtsText } from '../../utils';
import { useSpeechSynthesis } from '../../hooks';
import clsx from 'clsx';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface MessageItemProps {
  message: Message;
  autoSpeak?: boolean;
}

export function MessageItem({ message, autoSpeak = false }: MessageItemProps) {
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
  } = message;
  const [hasAutoSpoken, setHasAutoSpoken] = useState(false);
  const [isAudioPlaying, setIsAudioPlaying] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);
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

  // 工具调用/结果消息
  if (role === 'tool') {
    return (
      <ToolCallDisplay
        toolCall={toolCall}
        toolResult={toolResult}
      />
    );
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
	       const [, jsonStr] = content.split('team.event:');
	       try {
	         const payload = JSON.parse(jsonStr);
	         
	         const event = payload.event || payload.payload?.event;
	         
	         if (event) {
	           const { type, from_member, to_member, content: messageContent, timestamp: eventTimestamp } = event;
	           
	           const isP2P = type?.endsWith('.p2p');
	           const isBroadcast = type?.endsWith('.broadcast');
	           const isTeamLeaderMessage = from_member === 'team_leader' && !isP2P && !isBroadcast;
	           
	           const formatEventTime = (ts: number) => {
	             if (!ts) return '';
	             const date = new Date(ts);
	             return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
	           };
	           
	           // team_leader 发给用户的消息（不是 p2p 也不是 broadcast）
	           if (isTeamLeaderMessage) {
	             return (
	               <div className="flex justify-center my-2 animate-fade-in">
	                 <div className="max-w-[85%] w-full rounded-xl border border-border bg-secondary/50 p-3 shadow-sm">
	                   <div className="flex items-center gap-2 mb-1.5">
	                     <div className="w-2 h-2 rounded-full bg-accent" />
	                     <span className="font-medium text-sm text-text">
	                       {from_member || 'Unknown'}
	                     </span>
	                     <span className="text-xs text-text-muted">
	                       {formatEventTime(eventTimestamp)}
	                     </span>
	                   </div>
	                   <div className="text-sm text-text leading-relaxed">
	                     <span>{messageContent}</span>
	                   </div>
	                 </div>
	               </div>
	             );
	           }
	           
	           // p2p 和 broadcast 消息展示
	           return (
	             <div className="flex justify-center my-2 animate-fade-in">
	               <div className="max-w-[85%] w-full rounded-xl border border-border bg-secondary/50 p-3 shadow-sm">
	                 <div className="flex items-center gap-2 mb-1.5">
	                   <div className="w-2 h-2 rounded-full bg-accent" />
	                   <span className="font-medium text-sm text-text">
	                     {from_member || 'Unknown'}
	                   </span>
	                   <span className="text-xs text-text-muted">
	                     {formatEventTime(eventTimestamp)}
	                   </span>
	                 </div>
	                 <div className="text-sm text-text leading-relaxed">
	                   {isP2P && to_member && (
	                     <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-info/20 text-info text-xs mr-1.5">
	                       @{to_member}
	                     </span>
	                   )}
	                   {isBroadcast && (
	                     <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-warning/20 text-warning text-xs mr-1.5">
	                       @所有人
	                     </span>
	                   )}
	                   <span>{messageContent}</span>
	                 </div>
	               </div>
	             </div>
	           );
	         }
	       } catch (e) {
	         return (
	           <div className="flex justify-center my-4 animate-fade-in">
	             <div className="px-4 py-2 rounded-full bg-secondary border border-border text-text-muted text-sm">
	               {content}
	             </div>
	           </div>
	         );
	       }
	     }
	     
	     // 检查是否为 team_leader 消息（通过 ID 判断）
	     const isTeamLeaderMsg = id && id.startsWith('team-leader-');
	     
	     if (isTeamLeaderMsg) {
	       let messageContent = content;
	       let eventTimestamp: number | undefined;
	       
	       if (content.startsWith('team.leader:')) {
	         const [, jsonStr] = content.split('team.leader:');
	         try {
	           const data = JSON.parse(jsonStr);
	           messageContent = data.content;
	           eventTimestamp = data.timestamp;
	         } catch (e) {
	         }
	       }
	       
	       const formatEventTime = (ts: number | undefined) => {
	         if (!ts) return '';
	         const date = new Date(ts);
	         return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
	       };
	       
	       return (
	         <div className="flex justify-center my-2 animate-fade-in">
	           <div className="max-w-[85%] w-full rounded-xl border border-border bg-secondary/50 p-3 shadow-sm">
	             <div className="flex items-center gap-2 mb-1.5">
	               <div className="w-2 h-2 rounded-full bg-accent" />
	               <span className="font-medium text-sm text-text">
	                 team_leader
	               </span>
	               <span className="text-xs text-text-muted">
	                 {formatEventTime(eventTimestamp)}
	               </span>
	             </div>
	             <div className="text-sm text-text leading-relaxed whitespace-pre-wrap">
	               <span>{messageContent}</span>
	             </div>
	           </div>
	         </div>
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
  const showTTS = Boolean(
    !isUser && !isStreaming && content && (ttsSupported || audioBase64)
  );
  const showCopy = Boolean(content) && !isStreaming;
  const isPlaying = audioBase64 ? isAudioPlaying : isSpeaking;

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
                ${message.usageSummary.input_cost?.toFixed(4)} in /{' '}
                ${message.usageSummary.output_cost?.toFixed(4)} out /{' '}
                ${message.usageSummary.total_cost.toFixed(4)} total
              </span>
            )}
          </div>
        )}

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