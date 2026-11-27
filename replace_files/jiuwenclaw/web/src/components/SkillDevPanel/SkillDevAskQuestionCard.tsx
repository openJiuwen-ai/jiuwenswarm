/**
 * SkillDevAskQuestionCard - Agent ask_user_question 内联卡片
 *
 * 在 SkillDev 聊天流内以内联卡片形式展示 Agent 提出的问题，
 * 参考对话界面的 InlineQuestionCard 设计。
 *
 * - 每个问题都支持自定义文本输入
 * - multi_select=true 时选项可多选（toggle）
 * - multi_select=false 时选项单选
 * - 多问题模式提供「全部接受」快捷操作
 * - 统一通过底部按钮提交
 */

import { useState, useCallback, useMemo, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import type { AskUserQuestionPayload, UserAnswer } from '../../types';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

interface SkillDevAskQuestionCardProps {
  question: AskUserQuestionPayload;
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => void;
  onDismiss?: () => void;
}

export function SkillDevAskQuestionCard({
  question,
  onSubmit,
}: SkillDevAskQuestionCardProps) {
  const { t } = useTranslation();
  const [selections, setSelections] = useState<Map<number, Set<string>>>(new Map());
  const [customInputs, setCustomInputs] = useState<Map<number, string>>(new Map());
  const [submitted, setSubmitted] = useState(false);

  const requestId = question.request_id;
  useEffect(() => {
    setSelections(new Map());
    setCustomInputs(new Map());
    setSubmitted(false);
  }, [requestId]);

  const isBatch = question.questions.length > 1;

  const allAnswered = useMemo(() => {
    return question.questions.every((_, idx) => {
      const sel = selections.get(idx);
      return (sel && sel.size > 0) || customInputs.get(idx)?.trim();
    });
  }, [question, selections, customInputs]);

  const buildAnswers = useCallback(
    (selMap: Map<number, Set<string>>, inputMap: Map<number, string>): UserAnswer[] => {
      return question.questions.map((q, idx) => {
        const sel = selMap.get(idx);
        const custom = inputMap.get(idx)?.trim();
        const selected = sel && sel.size > 0
          ? Array.from(sel)
          : (custom ? [] : (q.options.length > 0 ? [q.options[0].label] : []));
        return {
          selected_options: selected,
          ...(custom ? { custom_input: custom } : {}),
        };
      });
    },
    [question]
  );

  const doSubmit = useCallback(
    (selMap: Map<number, Set<string>>, inputMap: Map<number, string>) => {
      setSubmitted(true);
      onSubmit(question.request_id, buildAnswers(selMap, inputMap), question.source);
    },
    [question, buildAnswers, onSubmit]
  );

  const handleSelect = useCallback(
    (questionIndex: number, optionLabel: string, multiSelect: boolean) => {
      if (submitted) return;
      setSelections((prev) => {
        const next = new Map(prev);
        const current = new Set(next.get(questionIndex) || []);
        if (multiSelect) {
          if (current.has(optionLabel)) {
            current.delete(optionLabel);
          } else {
            current.add(optionLabel);
          }
        } else {
          current.clear();
          current.add(optionLabel);
        }
        next.set(questionIndex, current);
        return next;
      });
      if (!multiSelect) {
        setCustomInputs((prev) => {
          const next = new Map(prev);
          next.delete(questionIndex);
          return next;
        });
      }
    },
    [submitted]
  );

  const handleCustomInput = useCallback(
    (questionIndex: number, value: string, multiSelect: boolean) => {
      setCustomInputs((prev) => {
        const next = new Map(prev);
        next.set(questionIndex, value);
        return next;
      });
      if (!multiSelect) {
        setSelections((prev) => {
          const next = new Map(prev);
          next.delete(questionIndex);
          return next;
        });
      }
    },
    []
  );

  const handleAcceptAll = useCallback(() => {
    if (submitted) return;
    const acceptLabel = t('chatUi.inlineQuestion.accept');
    const all = new Map<number, Set<string>>();
    question.questions.forEach((_, idx) => all.set(idx, new Set([acceptLabel])));
    setSelections(all);
    doSubmit(all, customInputs);
  }, [submitted, question, t, doSubmit, customInputs]);

  const handleSubmitAll = useCallback(() => {
    if (!allAnswered || submitted) return;
    doSubmit(selections, customInputs);
  }, [allAnswered, submitted, selections, customInputs, doSubmit]);

  const isAskTool = question.source === 'ask_tool';
  const infoColor = 'var(--info, #3b82f6)';
  const borderColor = isAskTool ? infoColor : 'var(--accent)';
  const cardTitle = isAskTool
    ? t('chatUi.inlineQuestion.toolHeader', '工具审批')
    : (question.questions[0]?.header ?? t('chatUi.inlineQuestion.toolHeader', 'Agent 需要确认'));
  const countLabel = isAskTool
    ? t('chatUi.inlineQuestion.questionCount', { count: question.questions.length })
    : t('chatUi.inlineQuestion.entryCount', { count: question.questions.length });
  const showAcceptAll = isBatch && !submitted && !isAskTool;

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
                d="M8.625 9.75a3.375 3.375 0 116.75 0c0 1.473-.956 2.725-2.281 3.167-.6.2-1.094.78-1.094 1.413V15m.563 3h.008v.008h-.008V18z"
              />
            </svg>
            <span
              className="text-xs font-semibold"
              style={{ color: borderColor }}
            >
              {cardTitle}
            </span>
            {isBatch && (
              <span className="text-xs" style={{ color: 'var(--muted)' }}>
                {countLabel}
              </span>
            )}
          </div>
          {showAcceptAll && (
            <button
              onClick={handleAcceptAll}
              className="text-xs font-medium px-2.5 py-1 rounded-md transition-opacity hover:opacity-80"
              style={{
                color: 'white',
                background: 'linear-gradient(135deg, var(--ok), var(--accent))',
              }}
            >
              {t('chatUi.inlineQuestion.acceptAll', '全部接受')}
            </button>
          )}
        </div>

        {/* 超时提示 */}
        {isAskTool &&
        typeof question.expires_at_ms === 'number' &&
        Number.isFinite(question.expires_at_ms) ? (
          <div className="px-4 pb-2 pt-1 text-[11px] leading-snug" style={{ color: 'var(--muted)' }}>
            截止：{new Date(question.expires_at_ms).toLocaleString(undefined, { hour12: false })}
          </div>
        ) : null}

        {/* 问题列表 */}
        <div className="overflow-y-auto" style={{ maxHeight: '60vh' }}>
          {question.questions.map((q, qIndex) => {
            const selectedSet = selections.get(qIndex) || new Set<string>();
            const isMulti = !!q.multi_select;
            return (
              <div
                key={qIndex}
                style={qIndex > 0 ? { borderTop: '1px solid var(--border)' } : undefined}
              >
                {/* 问题正文 */}
                <div
                  className="px-4 pt-3 pb-2 text-sm prose prose-sm max-w-none prose-headings:font-semibold prose-headings:text-sm prose-ul:my-1 prose-li:my-0 prose-li:pl-1"
                  style={{ color: 'var(--text)' }}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {q.question}
                  </ReactMarkdown>
                  {isMulti && (
                    <span className="text-[11px] ml-1" style={{ color: 'var(--muted)' }}>
                      （可多选）
                    </span>
                  )}
                </div>

                {/* 选项按钮 */}
                <div className="px-4 pb-1 flex flex-col gap-2">
                  {q.options.map((option) => {
                    const isAccept = option.label === t('chatUi.inlineQuestion.accept')
                      || option.label === t('chatUi.inlineQuestion.allowOnce')
                      || option.label === '本次允许'
                      || option.label === '确认'
                      || option.label === '是';
                    const isReject = option.label === t('chatUi.inlineQuestion.reject')
                      || option.label === '拒绝'
                      || option.label === '否';
                    const isSelected = selectedSet.has(option.label);

                    return (
                      <button
                        key={option.label}
                        onClick={() => handleSelect(qIndex, option.label, isMulti)}
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
                          <div className="flex items-center gap-2">
                            {isMulti && (
                              <span
                                className="inline-flex items-center justify-center w-4 h-4 rounded border flex-shrink-0"
                                style={{
                                  borderColor: isSelected ? 'var(--accent)' : 'var(--border)',
                                  backgroundColor: isSelected ? 'var(--accent)' : 'transparent',
                                }}
                              >
                                {isSelected && (
                                  <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={3}>
                                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                                  </svg>
                                )}
                              </span>
                            )}
                            <span>{option.label}</span>
                          </div>
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

                {/* 自定义输入 */}
                <div className="px-4 pb-3 pt-1">
                  <textarea
                    value={customInputs.get(qIndex) || ''}
                    onChange={(e) => handleCustomInput(qIndex, e.target.value, isMulti)}
                    disabled={submitted}
                    placeholder={t('userQuestion.customPlaceholder', '输入自定义内容...')}
                    className="w-full px-3 py-2 text-sm rounded-lg resize-none focus:outline-none"
                    style={{
                      backgroundColor: 'var(--bg-elevated)',
                      border: '1px solid var(--border)',
                      color: 'var(--text)',
                      opacity: submitted ? 0.6 : 1,
                    }}
                    onFocus={(e) => { e.currentTarget.style.borderColor = 'var(--accent)'; }}
                    onBlur={(e) => { e.currentTarget.style.borderColor = 'var(--border)'; }}
                    rows={2}
                  />
                </div>
              </div>
            );
          })}
        </div>

        {/* 底部操作栏 */}
        {!submitted && (
          <div
            className="px-4 py-3 flex items-center justify-between"
            style={{
              borderTop: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
            }}
          >
            <span className="text-xs" style={{ color: 'var(--muted)' }}>
              {isBatch
                ? `${question.questions.filter((_, idx) => {
                    const sel = selections.get(idx);
                    return (sel && sel.size > 0) || customInputs.get(idx)?.trim();
                  }).length}/${question.questions.length}`
                : ''}
            </span>
            <button
              onClick={handleSubmitAll}
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
              {t('chatUi.inlineQuestion.submit', '提交')}
            </button>
          </div>
        )}

        {/* 已提交状态 */}
        {submitted && (
          <div
            className="px-4 py-2 text-xs text-center"
            style={{
              borderTop: '1px solid var(--border)',
              backgroundColor: 'var(--panel-strong)',
              color: 'var(--ok, #22c55e)',
            }}
          >
            ✓ {t('userQuestion.submitted', '已提交回答')}
          </div>
        )}
      </div>
    </div>
  );
}
