/**
 * SkillDevChat - SkillDev 聊天界面
 *
 * 完全复制 ChatPanel 的特性：
 * - Markdown 渲染
 * - 工具调用展示
 * - 流式内容展示
 * - 复制功能
 * - TTS 朗读
 * - 媒体展示
 * - 自动滚动
 * - 欢迎界面
 * - 文件上传
 */

import { useRef, useEffect, useCallback, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { SkillDevMessage } from '../../stores';
import type { MediaItem, ToolExecution } from '../../types';
import '../ChatPanel/ChatPanel.css';
import { SkillDevQuestionCard } from './SkillDevQuestionCard';
import { SkillDevAskQuestionCard } from './SkillDevAskQuestionCard';
import type { ClarifyQuestion, ClarifyAnswer, SkillDevPlan } from '../../types/skilldev';
import type { AskUserQuestionPayload, UserAnswer } from '../../types';
import { SkillDevTimeline } from './SkillDevTimeline';

interface SkillDevChatProps {
  messages: SkillDevMessage[];
  toolExecutions: ToolExecution[];
  inputValue: string;
  setInputValue: (value: string) => void;
  onSend: (files?: MediaItem[], toolSpecFiles?: MediaItem[], skillPackages?: MediaItem[]) => void;
  onImportSkill?: (file: MediaItem) => Promise<void> | void;
  canImportSkill?: boolean;
  importSkillBlockedReason?: string;
  isProcessing: boolean;
  isSuspended: boolean;
  clarifyQuestions?: ClarifyQuestion[] | null;
  isClarifySubmitted?: boolean;
  onClarifySubmit?: (answers: ClarifyAnswer[], questions: ClarifyQuestion[]) => void;
  // 审批确认回调 - 支持 messageId 以精确定位审批卡片
  onConfirm?: (
    action: string,
    data?: {
      plan?: SkillDevPlan;
      feedback?: string;
      answers?: ClarifyAnswer[];
      messageId?: string;
    }
  ) => Promise<void> | void;
  // Agent ask_user_question 卡片
  pendingQuestion?: AskUserQuestionPayload | null;
  onSubmitAnswer?: (requestId: string, answers: UserAnswer[], source?: string) => void;
  onDismissQuestion?: () => void;
  /** 恢复会话后可继续执行任务（skilldev.chat 续跑） */
  showResumeTask?: boolean;
  onResumeTask?: () => void | Promise<void>;
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

// 文件预览组件
function FilePreview({
  file,
  onRemove,
  variant = 'resource',
}: {
  file: MediaItem;
  onRemove: () => void;
  variant?: 'resource' | 'toolSpec';
}) {
  const borderClass = variant === 'toolSpec' ? 'border-accent' : 'border-border';
  return (
    <div className={`flex items-center gap-2 px-3 py-2 bg-secondary rounded-lg border ${borderClass}`}>
      <svg className="w-4 h-4 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M18.375 12.739l-7.693 7.693a4.5 4.5 0 01-6.364-6.364l10.94-10.94A3 3 0 1119.5 7.372L8.552 18.32m.009-.01l-.01.01m5.699-9.941l-7.81 7.81a1.5 1.5 0 002.112 2.13" />
      </svg>
      <span className="text-sm text-text truncate max-w-[150px]">{file.filename}</span>
      <button
        onClick={onRemove}
        className="p-1 rounded hover:bg-hover text-text-muted hover:text-danger transition-colors"
        title="移除文件"
      >
        <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>
  );
}

export function SkillDevChat({
  messages,
  toolExecutions,
  inputValue,
  setInputValue,
  onSend,
  onImportSkill,
  canImportSkill = true,
  importSkillBlockedReason,
  isProcessing,
  isSuspended,
  clarifyQuestions,
  isClarifySubmitted,
  onClarifySubmit,
  onConfirm,
  pendingQuestion,
  onSubmitAnswer,
  onDismissQuestion,
  showResumeTask = false,
  onResumeTask,
}: SkillDevChatProps) {
  const { t } = useTranslation();
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toolSpecInputRef = useRef<HTMLInputElement>(null);
  const skillPackageInputRef = useRef<HTMLInputElement>(null);
  const importSkillInputRef = useRef<HTMLInputElement>(null);
  const [selectedFiles, setSelectedFiles] = useState<MediaItem[]>([]);
  const [selectedToolSpecFiles, setSelectedToolSpecFiles] = useState<MediaItem[]>([]);
  const [selectedSkillPackages, setSelectedSkillPackages] = useState<MediaItem[]>([]);
  const [isDragging, setIsDragging] = useState(false);
  const [toastMessage, setToastMessage] = useState<string | null>(null);
  const [isImportingSkill, setIsImportingSkill] = useState(false);
  const [isPickingImportSkill, setIsPickingImportSkill] = useState(false);
  const toastTimerRef = useRef<number | null>(null);
  const importPickTimerRef = useRef<number | null>(null);
  const showToast = useCallback((message: string) => {
    setToastMessage(message);
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
    }
    toastTimerRef.current = window.setTimeout(() => {
      setToastMessage(null);
      toastTimerRef.current = null;
    }, 2500);
  }, []);

  useEffect(() => {
    return () => {
      if (toastTimerRef.current !== null) {
        window.clearTimeout(toastTimerRef.current);
      }
      if (importPickTimerRef.current !== null) {
        window.clearTimeout(importPickTimerRef.current);
      }
    };
  }, []);


  // 跟踪用户是否正在查看历史消息（不在底部）
  const userScrolledUpRef = useRef(false);

  // 检测用户滚动位置
  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;

    // 检查是否在底部（有 40px 的阈值）
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    userScrolledUpRef.current = !atBottom;
  }, []);

  // 自动滚动到底部
  useEffect(() => {
    // 只有当用户在底部时才自动滚动
    if (!userScrolledUpRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages, toolExecutions]);

  // 自动调整 textarea 高度
  const adjustTextareaHeight = () => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 200)}px`;
    }
  };

  useEffect(() => {
    adjustTextareaHeight();
  }, [inputValue]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = () => {
    if (
      !inputValue.trim() &&
      selectedFiles.length === 0 &&
      selectedToolSpecFiles.length === 0 &&
      selectedSkillPackages.length === 0
    ) {
      return;
    }
    onSend(
      selectedFiles.length > 0 ? selectedFiles : undefined,
      selectedToolSpecFiles.length > 0 ? selectedToolSpecFiles : undefined,
      selectedSkillPackages.length > 0 ? selectedSkillPackages : undefined
    );
    setInputValue('');
    setSelectedFiles([]);
    setSelectedToolSpecFiles([]);
    setSelectedSkillPackages([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  // 文件处理
  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    processFiles(Array.from(files));
    e.target.value = ''; // 重置 input
  };

  const readFilesAsMedia = (files: File[], target: 'resource' | 'toolSpec' | 'skillPackage') => {
    files.forEach((file) => {
      const reader = new FileReader();
      reader.onload = (e) => {
        const base64 = e.target?.result as string;
        if (base64) {
          const base64Data = base64.split(',')[1];
          const mediaItem: MediaItem = {
            type: getFileType(file.type),
            mimeType: file.type || 'application/octet-stream',
            filename: file.name,
            base64Data,
          };
          if (target === 'resource') {
            setSelectedFiles((prev) => [...prev, mediaItem]);
          } else if (target === 'toolSpec') {
            setSelectedToolSpecFiles((prev) => [...prev, mediaItem]);
          } else {
            setSelectedSkillPackages((prev) => [...prev, mediaItem]);
          }
        }
      };
      reader.readAsDataURL(file);
    });
  };

  const processFiles = (files: File[]) => readFilesAsMedia(files, 'resource');

  const getFileType = (mimeType: string): MediaItem['type'] => {
    if (mimeType.startsWith('image/')) return 'image';
    if (mimeType.startsWith('audio/')) return 'audio';
    if (mimeType.startsWith('video/')) return 'video';
    return 'document';
  };

  const handleRemoveFile = (index: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleToolSpecSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    readFilesAsMedia(Array.from(files), 'toolSpec');
    e.target.value = '';
  };

  const handleSkillPackageSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    const picked = Array.from(files);
    const invalid = picked.find((file) => {
      const lower = file.name.toLowerCase();
      return !(lower.endsWith('.zip') || lower.endsWith('.skill'));
    });
    if (invalid) {
      showToast(t('skilldev.importSkillInvalidType'));
      e.target.value = '';
      return;
    }
    readFilesAsMedia(picked, 'skillPackage');
    e.target.value = '';
  };

  const handleImportSkillSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] ?? null;
    setIsPickingImportSkill(false);
    e.target.value = '';
    if (!file) {
      showToast(t('skilldev.importSkillNoFileSelected'));
      return;
    }
    if (!onImportSkill) {
      console.warn('[SkillDevChat] import handler missing');
      showToast(t('skilldev.importSkillFailed'));
      return;
    }

    const lowerName = file.name.toLowerCase();
    console.info('[SkillDevChat] import file selected', {
      filename: file.name,
      size: file.size,
      type: file.type,
    });
    if (!(lowerName.endsWith('.zip') || lowerName.endsWith('.skill'))) {
      showToast(t('skilldev.importSkillInvalidType'));
      return;
    }

    const reader = new FileReader();
    reader.onerror = () => {
      console.error('[SkillDevChat] failed to read import file', {
        filename: file.name,
      });
      showToast(t('skilldev.importSkillReadFailed'));
    };
    reader.onload = async (evt) => {
      const base64 = evt.target?.result as string;
      if (!base64) {
        showToast(t('skilldev.importSkillReadFailed'));
        return;
      }
      const base64Data = base64.split(',')[1];
      if (!base64Data) {
        showToast(t('skilldev.importSkillReadFailed'));
        return;
      }
      const mediaItem: MediaItem = {
        type: 'document',
        mimeType: file.type || 'application/octet-stream',
        filename: file.name,
        base64Data,
      };
      try {
        setIsImportingSkill(true);
        await onImportSkill(mediaItem);
      } finally {
        setIsImportingSkill(false);
      }
    };
    reader.readAsDataURL(file);
  };

  const handleImportSkillClick = () => {
    if (!canImportSkill) {
      showToast(importSkillBlockedReason || t('skilldev.importSkillBlocked'));
      return;
    }
    const input = importSkillInputRef.current;
    if (!input) {
      showToast(t('skilldev.importSkillInputUnavailable'));
      return;
    }
    console.info('[SkillDevChat] import button clicked');
    input.value = '';
    setIsPickingImportSkill(true);
    if (importPickTimerRef.current !== null) {
      window.clearTimeout(importPickTimerRef.current);
    }
    importPickTimerRef.current = window.setTimeout(() => {
      setIsPickingImportSkill(false);
      importPickTimerRef.current = null;
    }, 12000);
    input.click();
  };

  const handleRemoveToolSpec = (index: number) => {
    setSelectedToolSpecFiles((prev) => prev.filter((_, i) => i !== index));
  };

  const handleRemoveSkillPackage = (index: number) => {
    setSelectedSkillPackages((prev) => prev.filter((_, i) => i !== index));
  };

  // 拖拽处理
  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const files = Array.from(e.dataTransfer.files);
    processFiles(files);
  };

  const suggestions = [
    t('skilldev.example1'),
    t('skilldev.example2'),
  ];

  const handleSuggestion = useCallback((text: string) => {
    setInputValue(text);
  }, [setInputValue]);

  const canSend =
    (
      inputValue.trim() ||
      selectedFiles.length > 0 ||
      selectedToolSpecFiles.length > 0 ||
      selectedSkillPackages.length > 0
    ) &&
    !showResumeTask &&
    !isProcessing &&
    !isSuspended;

  return (
    <div className="flex flex-col h-full" data-testid="skilldev-chat">
      {/* 消息列表 */}
      <div
        ref={scrollContainerRef}
        className="flex-1 overflow-y-auto px-3 py-4"
        onScroll={handleScroll}
      >
        {messages.length === 0 && toolExecutions.length === 0 ? (
          <div className="chat-welcome">
            <div className="text-4xl mb-4">🛠️</div>
            <h2 className="chat-welcome__heading">{t('skilldev.welcomeTitle')}</h2>
            <p className="chat-welcome__subtext">
              {t('skilldev.welcomeDesc')}
            </p>
            <div className="chat-suggestions">
              {suggestions.map((text) => (
                <SuggestionCard key={text} text={text} onClick={() => handleSuggestion(text)} />
              ))}
            </div>
          </div>
        ) : (
          <>
            <SkillDevTimeline messages={messages} toolExecutions={toolExecutions} onConfirm={onConfirm} />
            {/* 思考中指示器 */}
            {isProcessing && <ThinkingIndicator />}
            {/* 问题澄清卡片 - 内联展示 */}
            {clarifyQuestions && clarifyQuestions.length > 0 && !isClarifySubmitted && onClarifySubmit && (
              <SkillDevQuestionCard
                questions={clarifyQuestions}
                onSubmit={onClarifySubmit}
                disabled={!isSuspended}
              />
            )}
            {/* Agent ask_user_question 卡片 - 内联展示 */}
            {pendingQuestion && onSubmitAnswer && (
              <SkillDevAskQuestionCard
                question={pendingQuestion}
                onSubmit={onSubmitAnswer}
                onDismiss={onDismissQuestion}
              />
            )}
          </>
        )}
        <div ref={messagesEndRef} />
      </div>

      {showResumeTask && onResumeTask && (
        <div className="mx-3 mb-2 px-3 py-3 rounded-lg border border-accent bg-accent-subtle flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-text">{t('skilldev.resumeTaskHint')}</p>
          <button
            type="button"
            onClick={() => void onResumeTask()}
            disabled={isProcessing}
            className="shrink-0 px-4 py-2 rounded-md text-sm font-medium bg-accent text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
          >
            {t('skilldev.resumeTask')}
          </button>
        </div>
      )}

      {/* 输入框 - 参考 Chat 页面 chat-input-container 样式 */}
      <div className="chat-compose px-3 pb-4">
        <div className="mb-2 px-3 py-2 rounded-md border border-border bg-secondary flex items-center justify-between gap-3">
          <span className="text-xs text-text-muted">
            {t('skilldev.importSkillHint')}
          </span>
          <button
            type="button"
            onClick={handleImportSkillClick}
            disabled={isProcessing || isSuspended || isImportingSkill}
            className="px-3 py-1.5 rounded-md text-xs font-medium bg-accent text-white hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed transition-opacity"
            title={t('skilldev.importSkill')}
          >
            {isImportingSkill
              ? t('skilldev.importSkillLoading')
              : isPickingImportSkill
                ? t('skilldev.importSkillPicking')
                : t('skilldev.importSkill')}
          </button>
          <input
            ref={importSkillInputRef}
            type="file"
            accept=".zip,.skill,application/zip,application/octet-stream"
            onChange={handleImportSkillSelect}
            className="hidden"
          />
        </div>
        <div
          className={`chat-input-container ${isDragging ? 'border-accent bg-accent-subtle' : ''}`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {/* 文件预览区域 */}
          {(selectedFiles.length > 0 || selectedToolSpecFiles.length > 0 || selectedSkillPackages.length > 0) && (
            <div className="px-4 pt-3 pb-2 border-b border-border flex flex-col gap-2">
              {selectedFiles.length > 0 && (
                <div className="flex flex-wrap gap-2 items-center">
                  <span className="text-xs text-text-muted shrink-0">{t('skilldev.filesAttached')}</span>
                  {selectedFiles.map((file, index) => (
                    <FilePreview
                      key={`res-${file.filename}-${index}`}
                      file={file}
                      variant="resource"
                      onRemove={() => handleRemoveFile(index)}
                    />
                  ))}
                </div>
              )}
              {selectedToolSpecFiles.length > 0 && (
                <div className="flex flex-wrap gap-2 items-center">
                  <span className="text-xs text-text-muted shrink-0">{t('skilldev.toolSpecsAttached')}</span>
                  {selectedToolSpecFiles.map((file, index) => (
                    <FilePreview
                      key={`ts-${file.filename}-${index}`}
                      file={file}
                      variant="toolSpec"
                      onRemove={() => handleRemoveToolSpec(index)}
                    />
                  ))}
                </div>
              )}
              {selectedSkillPackages.length > 0 && (
                <div className="flex flex-wrap gap-2 items-center">
                  <span className="text-xs text-text-muted shrink-0">{t('skilldev.skillPackagesAttached')}</span>
                  {selectedSkillPackages.map((file, index) => (
                    <FilePreview
                      key={`sp-${file.filename}-${index}`}
                      file={file}
                      variant="resource"
                      onRemove={() => handleRemoveSkillPackage(index)}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          <textarea
            ref={textareaRef}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              showResumeTask
                ? t('skilldev.resumeTaskHint')
                : isSuspended
                ? t('skilldev.waitingInput')
                : isProcessing
                ? t('skilldev.processing')
                : t('skilldev.inputPlaceholder')
            }
            disabled={showResumeTask || isProcessing || isSuspended}
            className="chat-input-textarea"
            rows={1}
          />
          <div className="chat-input-toolbar">
            <div className="chat-input-toolbar-left flex items-center gap-2">
              <div className="chat-input-hints">
                <span>Enter {t('common.send')}</span>
              </div>
            </div>
            <div className="chat-input-actions">
              {/* 文件上传按钮 */}
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={showResumeTask || isProcessing || isSuspended}
                className="chat-input-btn"
                title="上传文件"
              >
                <svg className="chat-input-btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M18.375 12.739l-7.693 7.693a4.5 4.5 0 01-6.364-6.364l10.94-10.94A3 3 0 1119.5 7.372L8.552 18.32m.009-.01l-.01.01m5.699-9.941l-7.81 7.81a1.5 1.5 0 002.112 2.13" />
                </svg>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                onChange={handleFileSelect}
                className="hidden"
              />
              <button
                type="button"
                onClick={() => toolSpecInputRef.current?.click()}
                disabled={showResumeTask || isProcessing || isSuspended}
                className="chat-input-btn"
                title={t('skilldev.uploadToolSpecsTitle')}
              >
                <svg className="chat-input-btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
                  />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
              </button>
              <input
                ref={toolSpecInputRef}
                type="file"
                multiple
                accept=".json,.yaml,.yml,.md,.txt,application/json,text/yaml,text/markdown,text/plain"
                onChange={handleToolSpecSelect}
                className="hidden"
              />
              <button
                type="button"
                onClick={() => skillPackageInputRef.current?.click()}
                disabled={showResumeTask || isProcessing || isSuspended}
                className="chat-input-btn"
                title={t('skilldev.uploadSkillPackagesTitle')}
              >
                <svg className="chat-input-btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V6m0 0l-3.75 3.75M12 6l3.75 3.75M4.5 16.5v1.125A2.625 2.625 0 007.125 20.25h9.75A2.625 2.625 0 0019.5 17.625V16.5" />
                </svg>
              </button>
              <input
                ref={skillPackageInputRef}
                type="file"
                multiple
                accept=".zip,.skill,application/zip,application/octet-stream"
                onChange={handleSkillPackageSelect}
                className="hidden"
              />
              <button
                type="button"
                onClick={handleSend}
                disabled={!canSend}
                className={`chat-input-btn chat-input-btn--send ${
                  canSend ? 'chat-input-btn--send-active' : ''
                }`}
              >
                <svg className="chat-input-btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12z" />
                </svg>
              </button>
            </div>
          </div>
        </div>
        <p className="text-xs text-text-muted mt-2 px-1">
          {isDragging
            ? '释放以上传文件'
            : isSuspended
            ? t('skilldev.suspendedHint')
            : isProcessing
            ? t('skilldev.processingHint')
            : t('skilldev.inputHint')}
        </p>
        {toastMessage && (
          <div className="mt-2 px-3 py-2 text-xs rounded-md bg-danger-subtle text-danger border border-danger/30 animate-rise">
            {toastMessage}
          </div>
        )}
      </div>
    </div>
  );
}
