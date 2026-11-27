/**
 * InlineQuestionCard 组件
 *
 * 在聊天流内以内联卡片形式展示用户审批请求（接收/拒绝），
 * 替代全屏大弹窗（UserQuestionModal）。
 *
 * 单问题模式：点击选项后立即提交。
 * 多问题模式（批量审批）：逐条选择后统一提交，并提供"全部接收"快捷操作。
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores';
import { UserAnswer } from '../../types';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface InlineQuestionCardProps {
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => void;
}

export function InlineQuestionCard({ onSubmit }: InlineQuestionCardProps) {
  const { t } = useTranslation();
  const { pendingQuestion, setPendingQuestion } = useChatStore();
  const [selections, setSelections] = useState<Map<number, string>>(new Map());
  const [submitted, setSubmitted] = useState(false);

  const requestId = pendingQuestion?.request_id;
  useEffect(() => {
    setSelections(new Map());
    setSubmitted(false);
  }, [requestId]);

  const isBatch = (pendingQuestion?.questions.length ?? 0) > 1;

  const allAnswered = useMemo(() => {
    if (!pendingQuestion) return false;
    return pendingQuestion.questions.every((_, idx) => selections.has(idx));
  }, [pendingQuestion, selections]);

  const buildAnswers = useCallback(
    (selMap: Map<number, string>): UserAnswer[] => {
      return (pendingQuestion?.questions ?? []).map((q, idx) => {
        const sel = selMap.get(idx);
        if (sel) return { selected_options: [sel] };
        return { selected_options: q.options.length > 0 ? [q.options[0].label] : [] };
      });
    },
    [pendingQuestion]
  );

  const doSubmit = useCallback(
    (selMap: Map<number, string>) => {
      if (!pendingQuestion) return;
      setSubmitted(true);
      onSubmit(pendingQuestion.request_id, buildAnswers(selMap), pendingQuestion.source);
      setPendingQuestion(null);
    },
    [pendingQuestion, buildAnswers, onSubmit, setPendingQuestion]
  );

  const handleSelect = useCallback(
    (questionIndex: number, optionLabel: string) => {
      if (submitted) return;

      const next = new Map(selections);
      next.set(questionIndex, optionLabel);
      setSelections(next);

      if (!isBatch) {
        doSubmit(next);
      }
    },
    [submitted, selections, isBatch, doSubmit]
  );

  const handleAcceptAll = useCallback(() => {
    if (!pendingQuestion || submitted) return;
    const acceptLabel = t('chatUi.inlineQuestion.accept');
    const all = new Map<number, string>();
    pendingQuestion.questions.forEach((_, idx) => all.set(idx, acceptLabel));
    setSelections(all);
    doSubmit(all);
  }, [pendingQuestion, submitted, t, doSubmit]);

  const handleSubmitBatch = useCallback(() => {
    if (!allAnswered || submitted) return;
    doSubmit(selections);
  }, [allAnswered, submitted, selections, doSubmit]);

  // Support skill evolution (skill_evolve_*) and new skill creation (skill_create_*)
  const isEvolution = (pendingQuestion?.request_id?.startsWith('skill_evolve_') ||
                       pendingQuestion?.request_id?.startsWith('skill_create_')) ?? false;

  if (!pendingQuestion) {
    return null;
  }

  const borderColor = isEvolution
    ? 'var(--warning, #f59e0b)'
    : 'var(--accent)';

  return (
    <div className="animate-rise mx-2 my-3">
      <div
        className="w-full rounded-xl overflow-hidden"
        style={{
          border: `1px solid ${borderColor}`,
          backgroundColor: 'var(--card)',
        }}
      >
        {/* 标题行 */}
        <div
          className="px-4 py-2.5 flex items-center justify-between"
          style={{
            borderBottom: '1px solid var(--border)',
            backgroundColor: 'var(--panel-strong)',
          }}
        >
          <div className="flex items-center gap-2">
            {isEvolution ? (
              <svg
                className="w-3.5 h-3.5 flex-shrink-0"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                strokeWidth={2}
                style={{ color: borderColor }}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z"
                />
              </svg>
            ) : (
              <svg
                className="w-3.5 h-3.5 flex-shrink-0"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                strokeWidth={2}
                style={{ color: 'var(--accent)' }}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23-.693L5 14.5m14.8.8l1.402 1.402c1.232 1.232.65 3.318-1.067 3.611A48.309 48.309 0 0112 21c-2.773 0-5.491-.235-8.135-.687-1.718-.293-2.3-2.379-1.067-3.61L5 14.5"
                />
              </svg>
            )}
            <span
              className="text-xs font-semibold"
              style={{ color: isEvolution ? borderColor : 'var(--accent)' }}
            >
              {pendingQuestion.questions[0]?.header ?? t('chatUi.inlineQuestion.header')}
            </span>
            {isBatch && (
              <span
                className="text-xs"
                style={{ color: 'var(--muted)' }}
              >
                {t('chatUi.inlineQuestion.entryCount', { count: pendingQuestion.questions.length })}
              </span>
            )}
          </div>
          {isBatch && !submitted && (
            <button
              onClick={handleAcceptAll}
              className="text-xs font-medium px-2.5 py-1 rounded-md transition-opacity hover:opacity-80"
              style={{
                color: 'white',
                background: 'linear-gradient(135deg, var(--ok), var(--accent))',
              }}
            >
              {t('chatUi.inlineQuestion.acceptAll')}
            </button>
          )}
        </div>

        {/* 问题列表 */}
        <div
          className="overflow-y-auto"
          style={{ maxHeight: '60vh' }}
        >
          {pendingQuestion.questions.map((question, qIndex) => {
            const selectedLabel = selections.get(qIndex);

            return (
              <div
                key={qIndex}
                style={
                  qIndex > 0
                    ? { borderTop: '1px solid var(--border)' }
                    : undefined
                }
              >
                {/* 问题正文 */}
                <div
                  className="px-4 pt-3 pb-2 text-sm prose prose-sm max-w-none prose-headings:font-semibold prose-headings:text-sm prose-ul:my-1 prose-li:my-0 prose-li:pl-1"
                  style={{ color: 'var(--text)' }}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {question.question}
                  </ReactMarkdown>
                </div>

                {/* 选项按钮 */}
                <div className="px-4 pb-3 flex flex-col gap-2">
                  {question.options.map((option) => {
                    const isAccept = option.label === t('chatUi.inlineQuestion.accept')
                      || option.label === t('chatUi.inlineQuestion.allowOnce')
                      || option.label === '本次允许';
                    const isAlwaysAllow = option.label === t('chatUi.inlineQuestion.alwaysAllow')
                      || option.label === '总是允许';
                    const isReject = option.label === t('chatUi.inlineQuestion.reject')
                      || option.label === '拒绝';
                    const isSelected = selectedLabel === option.label;

                    return (
                      <button
                        key={option.label}
                        onClick={() => handleSelect(qIndex, option.label)}
                        disabled={submitted}
                        className="w-full text-left px-4 py-2.5 text-sm font-medium rounded-lg transition-all"
                        style={{
                          backgroundColor: isSelected
                            ? (isAccept
                                ? 'var(--ok-subtle, rgba(34,197,94,0.12))'
                                : isReject
                                  ? 'var(--danger-subtle, rgba(239,68,68,0.12))'
                                  : 'var(--accent-subtle)')
                            : 'var(--bg-elevated)',
                          border: `1px solid ${
                            isSelected
                              ? (isAccept ? 'var(--ok)' : isReject ? 'var(--danger)' : 'var(--accent)')
                              : 'var(--border)'
                          }`,
                          color: isSelected
                            ? (isAccept ? 'var(--ok)' : isReject ? 'var(--danger)' : 'var(--text-strong)')
                            : 'var(--text)',
                          opacity: submitted ? 0.6 : 1,
                          cursor: submitted ? 'default' : 'pointer',
                        }}
                        onMouseOver={(e) => {
                          if (submitted || isSelected) return;
                          const el = e.currentTarget;
                          if (isAccept) {
                            el.style.backgroundColor = 'var(--ok-subtle, rgba(34,197,94,0.12))';
                            el.style.borderColor = 'var(--ok)';
                            el.style.color = 'var(--ok)';
                          } else if (isAlwaysAllow) {
                            el.style.backgroundColor = 'var(--accent-subtle, rgba(59,130,246,0.12))';
                            el.style.borderColor = 'var(--accent)';
                            el.style.color = 'var(--accent)';
                          } else if (isReject) {
                            el.style.backgroundColor = 'var(--danger-subtle, rgba(239,68,68,0.12))';
                            el.style.borderColor = 'var(--danger)';
                            el.style.color = 'var(--danger)';
                          } else {
                            el.style.backgroundColor = 'var(--bg-hover)';
                            el.style.borderColor = 'var(--border-strong)';
                          }
                        }}
                        onMouseOut={(e) => {
                          if (submitted || isSelected) return;
                          const el = e.currentTarget;
                          el.style.backgroundColor = 'var(--bg-elevated)';
                          el.style.borderColor = 'var(--border)';
                          el.style.color = 'var(--text)';
                        }}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <span>{option.label}</span>
                          {option.description && (
                            <span className="text-xs font-normal" style={{ color: 'var(--muted)' }}>
                              {option.description}
                            </span>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        {/* 批量模式底部操作栏 */}
        {isBatch && !submitted && (
          <div
            className="px-4 py-3 flex items-center justify-between"
            style={{
              borderTop: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            <span
              className="text-xs"
              style={{ color: 'var(--muted)' }}
            >
              {selections.size}/{pendingQuestion.questions.length}
            </span>
            <button
              onClick={handleSubmitBatch}
              disabled={!allAnswered}
              className="px-4 py-1.5 text-xs font-medium text-white rounded-lg transition-opacity"
              style={{
                background: allAnswered
                  ? 'linear-gradient(135deg, var(--accent), var(--accent-2))'
                  : 'var(--border)',
                opacity: allAnswered ? 1 : 0.5,
                cursor: allAnswered ? 'pointer' : 'not-allowed',
              }}
            >
              {t('chatUi.inlineQuestion.submit')}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
