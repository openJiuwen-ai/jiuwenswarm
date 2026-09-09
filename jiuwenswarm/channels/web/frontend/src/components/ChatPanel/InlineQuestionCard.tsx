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
import { UserAnswer, QuestionOption, Question } from '../../types';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { classifyPrompt } from '../InteractionSlot/promptRouting';
import { isValidSkillApprovalCard, SkillApprovalCard } from './SkillApprovalCard';

interface InlineQuestionCardProps {
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => Promise<void>;
}

// 后端会给带选项的问题末尾追加一个「自定义输入」选项（interrupt_helpers._build_multi_questions）。
// 兼容两种形态：带哨兵值 __other__（新）或仅有英文文案 Other 且无 value（旧）。
const OTHER_VALUE = '__other__';
const isOtherOption = (option: QuestionOption): boolean =>
  option.value === OTHER_VALUE || (!option.value && option.label === 'Other');

function ApprovalQuestionContent({ question }: { question: Question }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]}>
      {question.question}
    </ReactMarkdown>
  );
}

export function InlineQuestionCard({ onSubmit }: InlineQuestionCardProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const pendingQuestion = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.pendingQuestion ?? null);
  const [selections, setSelections] = useState<Map<number, string>>(new Map());
  const [customInputs, setCustomInputs] = useState<Map<number, string>>(new Map());
  const [submitted, setSubmitted] = useState(false);

  const requestId = pendingQuestion?.request_id;
  useEffect(() => {
    setSelections(new Map());
    setCustomInputs(new Map());
    setSubmitted(false);
  }, [requestId]);

  const isBatch = (pendingQuestion?.questions.length ?? 0) > 1;

  const allAnswered = useMemo(() => {
    if (!pendingQuestion) return false;
    return pendingQuestion.questions.every((_, idx) => {
      if (!selections.has(idx)) return false;
      // 选了 Other 必须填了内容才算完成
      if (selections.get(idx) === OTHER_VALUE) {
        return (customInputs.get(idx) || '').trim().length > 0;
      }
      return true;
    });
  }, [pendingQuestion, selections, customInputs]);

  const buildAnswers = useCallback(
    (selMap: Map<number, string>): UserAnswer[] => {
      return (pendingQuestion?.questions ?? []).map((q, idx) => {
        const sel = selMap.get(idx);
        // Other：清空 selected_options，把用户输入放进 custom_input，
        // 命中后端 interface.py 的 `elif custom_input` 分支。
        if (sel === OTHER_VALUE) {
          return { question: q.question, selected_options: [], custom_input: (customInputs.get(idx) || '').trim() };
        }
        if (sel) return { question: q.question, selected_options: [sel] };
        const firstOption = q.options[0];
        return { question: q.question, selected_options: firstOption ? [firstOption.value || firstOption.label] : [] };
      });
    },
    [pendingQuestion, customInputs]
  );

  const doSubmit = useCallback(
    (selMap: Map<number, string>) => {
      if (!pendingQuestion) return;
      setSubmitted(true);
      onSubmit(pendingQuestion.request_id, buildAnswers(selMap), pendingQuestion.source);
      const sid = useChatStore.getState().activeSessionId;
      if (sid) {
        useChatStore.getState().setPendingQuestion(sid, null);
      }
    },
    [pendingQuestion, buildAnswers, onSubmit]
  );

  const handleSelect = useCallback(
    (questionIndex: number, option: QuestionOption) => {
      if (submitted) return;

      const isOther = isOtherOption(option);
      const value = isOther ? OTHER_VALUE : (option.value || option.label);
      const next = new Map(selections);
      next.set(questionIndex, value);
      setSelections(next);

      // Other：不立即提交，展开输入框，等用户填完再手动提交。
      if (isOther) return;

      if (!isBatch) {
        doSubmit(next);
      }
    },
    [submitted, selections, isBatch, doSubmit]
  );

  const handleCustomInputChange = useCallback(
    (questionIndex: number, text: string) => {
      setCustomInputs((prev) => new Map(prev).set(questionIndex, text));
    },
    []
  );

  // 单问题模式下，Other 输入完成后由此提交
  const handleSubmitOther = useCallback(
    (questionIndex: number) => {
      if (submitted) return;
      if ((customInputs.get(questionIndex) || '').trim().length === 0) return;
      doSubmit(selections);
    },
    [submitted, customInputs, selections, doSubmit]
  );

  const handleAcceptAll = useCallback(() => {
    if (!pendingQuestion || submitted) return;
    const all = new Map<number, string>();
    pendingQuestion.questions.forEach((q, idx) => {
      const acceptOption = q.options.find(o =>
        o.label === t('chatUi.inlineQuestion.accept') ||
        o.label === '本次允许'
      );
      all.set(idx, acceptOption?.value || acceptOption?.label || '');
    });
    setSelections(all);
    doSubmit(all);
  }, [pendingQuestion, submitted, t, doSubmit]);

  const handleSubmitBatch = useCallback(() => {
    if (!allAnswered || submitted) return;
    doSubmit(selections);
  }, [allAnswered, submitted, selections, doSubmit]);

  // Support skill evolution, team skill evolution, and new skill creation flows.
  const isEvolution = (
    pendingQuestion?.request_id?.startsWith('skill_evolve_') ||
    pendingQuestion?.request_id?.startsWith('team_skill_evolve_')
  ) ?? false;

  // Skill 加载审批卡：有结构化卡片时用 SkillApprovalCard 替换默认渲染
  const skillApprovalCard = isValidSkillApprovalCard(pendingQuestion?.skill_approval_card)
    ? pendingQuestion.skill_approval_card
    : null;

  // 授权 / 交互类弹窗改由输入框上方的 InteractionSlot 承载；
  // 此处仅保留 legacy（演进审批 / 计划审批 / Skill 加载审批）在消息流内渲染。
  if (!pendingQuestion || classifyPrompt(pendingQuestion) !== 'legacy') {
    return null;
  }

  if (skillApprovalCard) {
    return <SkillApprovalCard key={pendingQuestion.request_id} onSubmit={onSubmit} card={skillApprovalCard} />;
  }

  const borderColor = isEvolution
    ? 'var(--color-feedback-warning)'
    : 'var(--color-action-primary)';

  return (
    <div className="animate-rise mx-2 my-3">
      <div
        className="w-full rounded-xl overflow-hidden"
        style={{
          border: `1px solid ${borderColor}`,
          backgroundColor: 'var(--color-surface-card)',
        }}
      >
        {/* 标题行 */}
        <div
          className="px-4 py-2.5 flex items-center justify-between"
          style={{
            borderBottom: '1px solid var(--color-border-default)',
            backgroundColor: 'var(--color-surface-panel-strong)',
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
                style={{ color: 'var(--color-action-primary)' }}
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
              style={{ color: isEvolution ? borderColor : 'var(--color-action-primary)' }}
            >
              {pendingQuestion.questions[0]?.header ?? t('chatUi.inlineQuestion.header')}
            </span>
            {isBatch && (
              <span
                className="text-xs"
                style={{ color: 'var(--color-text-secondary)' }}
              >
                {t('chatUi.inlineQuestion.entryCount', { count: pendingQuestion.questions.length })}
              </span>
            )}
          </div>
          {isBatch && !submitted && (
            <button
              onClick={handleAcceptAll}
              className="text-xs font-medium px-2.5 py-1 rounded-md  hover:opacity-80"
              style={{
                color: 'white',
                background: 'linear-gradient(135deg, var(--color-feedback-success), var(--color-action-primary))',
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
            const selectedValue = selections.get(qIndex);

            return (
              <div
                key={qIndex}
                style={
                  qIndex > 0
                    ? { borderTop: '1px solid var(--color-border-default)' }
                    : undefined
                }
              >
                {/* 问题正文 */}
                <div
                  className="px-4 pt-3 pb-2 text-sm prose prose-sm max-w-none prose-headings:font-semibold prose-headings:text-sm prose-ul:my-1 prose-li:my-0 prose-li:pl-1"
                  style={{ color: 'var(--color-text-primary)' }}
                >
                  <ApprovalQuestionContent question={question} />
                </div>

                {/* 选项按钮 */}
                <div className="px-4 pb-3 flex flex-col gap-2">
                  {question.options.map((option) => {
                    const optionValue = isOtherOption(option) ? OTHER_VALUE : (option.value || option.label);
                    const isAccept = option.label === t('chatUi.inlineQuestion.accept')
                      || option.label === t('chatUi.inlineQuestion.allowOnce')
                      || option.label === '本次允许';
                    const isSessionAllow = option.label === t('chatUi.inlineQuestion.sessionAllow')
                      || option.label === '会话内记住';
                    const isAlwaysAllow = option.label === t('chatUi.inlineQuestion.alwaysAllow')
                      || option.label === '永久记住';
                    const isReject = option.label === t('chatUi.inlineQuestion.reject')
                      || option.label === '拒绝';
                    const isSelected = selectedValue === optionValue;

                    return (
                      <button
                        key={option.label}
                        onClick={() => handleSelect(qIndex, option)}
                        disabled={submitted}
                        className="w-full text-left px-4 py-2.5 text-sm font-medium rounded-lg "
                        style={{
                          backgroundColor: isSelected
                            ? (isAccept
                                ? 'var(--color-feedback-success-subtle)'
                                : isSessionAllow
                                  ? 'var(--color-feedback-info-subtle)'
                                  : isAlwaysAllow
                                    ? 'var(--color-action-primary-subtle)'
                                    : isReject
                                      ? 'var(--color-feedback-danger-subtle)'
                                      : 'var(--color-action-primary-subtle)')
                            : 'var(--color-surface-elevated)',
                          border: `1px solid ${
                            isSelected
                              ? (isAccept ? 'var(--color-feedback-success)' : isSessionAllow ? 'var(--color-feedback-info)' : isAlwaysAllow ? 'var(--color-action-primary)' : isReject ? 'var(--color-feedback-danger)' : 'var(--color-action-primary)')
                              : 'var(--color-border-default)'
                          }`,
                          color: isSelected
                            ? (isAccept ? 'var(--color-feedback-success)' : isSessionAllow ? 'var(--color-feedback-info)' : isAlwaysAllow ? 'var(--color-action-primary)' : isReject ? 'var(--color-feedback-danger)' : 'var(--color-text-strong)')
                            : 'var(--color-text-primary)',
                          opacity: submitted ? 0.6 : 1,
                          cursor: submitted ? 'default' : 'pointer',
                        }}
                          onMouseOver={(e) => {
                            if (submitted || isSelected) return;
                            const el = e.currentTarget;
                            if (isAccept) {
                              el.style.backgroundColor = 'var(--color-feedback-success-subtle)';
                              el.style.borderColor = 'var(--color-feedback-success)';
                              el.style.color = 'var(--color-feedback-success)';
                            } else if (isSessionAllow) {
                              el.style.backgroundColor = 'var(--color-feedback-info-subtle)';
                              el.style.borderColor = 'var(--color-feedback-info)';
                              el.style.color = 'var(--color-feedback-info)';
                            } else if (isAlwaysAllow) {
                              el.style.backgroundColor = 'var(--color-action-primary-subtle)';
                              el.style.borderColor = 'var(--color-action-primary)';
                              el.style.color = 'var(--color-action-primary)';
                            } else if (isReject) {
                              el.style.backgroundColor = 'var(--color-feedback-danger-subtle)';
                              el.style.borderColor = 'var(--color-feedback-danger)';
                              el.style.color = 'var(--color-feedback-danger)';
                            } else {
                              el.style.backgroundColor = 'var(--color-surface-hover)';
                              el.style.borderColor = 'var(--color-border-strong)';
                            }
                          }}
                        onMouseOut={(e) => {
                          if (submitted || isSelected) return;
                          const el = e.currentTarget;
                          el.style.backgroundColor = 'var(--color-surface-elevated)';
                          el.style.borderColor = 'var(--color-border-default)';
                          el.style.color = 'var(--color-text-primary)';
                        }}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <span>{option.label}</span>
                          {option.description && (
                            <span className="text-xs font-normal" style={{ color: 'var(--color-text-secondary)' }}>
                              {option.description}
                            </span>
                          )}
                        </div>
                      </button>
                    );
                  })}

                  {/* 选中 Other 后展开自定义输入框：填完再提交 */}
                  {selectedValue === OTHER_VALUE && (
                    <div className="mt-1 flex flex-col gap-2">
                      <textarea
                        autoFocus
                        value={customInputs.get(qIndex) || ''}
                        onChange={(e) => handleCustomInputChange(qIndex, e.target.value)}
                        placeholder={t('userQuestion.customPlaceholder')}
                        disabled={submitted}
                        rows={2}
                        className="w-full px-3 py-2 text-sm rounded-lg resize-none focus:outline-none"
                        style={{
                          backgroundColor: 'var(--color-surface-elevated)',
                          border: '1px solid var(--color-border-default)',
                          color: 'var(--color-text-primary)',
                        }}
                        onFocus={(e) => { e.currentTarget.style.borderColor = 'var(--color-border-focus)'; }}
                        onBlur={(e) => { e.currentTarget.style.borderColor = 'var(--color-border-default)'; }}
                        onKeyDown={(e) => {
                          if (!isBatch && e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                            e.preventDefault();
                            handleSubmitOther(qIndex);
                          }
                        }}
                      />
                      {!isBatch && (() => {
                        const hasText = (customInputs.get(qIndex) || '').trim().length > 0;
                        return (
                          <button
                            onClick={() => handleSubmitOther(qIndex)}
                            disabled={submitted || !hasText}
                            className="self-end px-4 py-1.5 text-xs font-medium text-white rounded-lg transition-opacity"
                            style={{
                              background: hasText
                                ? 'linear-gradient(135deg, var(--color-action-primary), var(--color-brand-secondary))'
                                : 'var(--color-border-default)',
                              opacity: hasText ? 1 : 0.5,
                              cursor: hasText ? 'pointer' : 'not-allowed',
                            }}
                          >
                            {t('chatUi.inlineQuestion.submit')}
                          </button>
                        );
                      })()}
                    </div>
                  )}
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
              borderTop: '1px solid var(--color-border-default)',
              backgroundColor: 'var(--color-surface-panel-strong)',
            }}
          >
            <span
              className="text-xs"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {selections.size}/{pendingQuestion.questions.length}
            </span>
            <button
              onClick={handleSubmitBatch}
              disabled={!allAnswered}
              className="px-4 py-1.5 text-xs font-medium text-text-inverse rounded-lg "
              style={{
                background: allAnswered
                  ? 'linear-gradient(135deg, var(--color-action-primary), var(--color-brand-secondary))'
                  : 'var(--color-border-default)',
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
