/**
 * 用户问题弹窗组件
 *
 * 显示 Agent 提出的问题，让用户选择或输入回答
 */

import { useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore } from '../../stores';
import { Question, UserAnswer } from '../../types';

interface UserQuestionModalProps {
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => void;
}

export function UserQuestionModal({ onSubmit }: UserQuestionModalProps) {
  const { t } = useTranslation();
  const pendingQuestion = useChatStore((s) => s.runtimes[s.activeSessionId ?? '']?.pendingQuestion ?? null);
  const setPendingQuestion = useChatStore((s) => s.setPendingQuestion);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const [answers, setAnswers] = useState<Map<number, UserAnswer>>(new Map());

  // 处理选项选择
  const handleOptionSelect = useCallback(
    (questionIndex: number, optionLabel: string, isMultiSelect: boolean) => {
      setAnswers((prev) => {
        const newAnswers = new Map(prev);
        const current = newAnswers.get(questionIndex) || {
          question: pendingQuestion?.questions[questionIndex]?.question,
          selected_options: [],
        };

        if (isMultiSelect) {
          // 多选：切换选项
          const selected = current.selected_options || [];
          const newSelected = selected.includes(optionLabel)
            ? selected.filter((o) => o !== optionLabel)
            : [...selected, optionLabel];
          newAnswers.set(questionIndex, {
            ...current,
            selected_options: newSelected,
          });
        } else {
          // 单选：替换选项
          newAnswers.set(questionIndex, {
            ...current,
            selected_options: [optionLabel],
          });
        }

        return newAnswers;
      });
    },
    [pendingQuestion]
  );

  // 处理自定义输入
  const handleCustomInput = useCallback(
    (questionIndex: number, value: string) => {
      setAnswers((prev) => {
        const newAnswers = new Map(prev);
        const current = newAnswers.get(questionIndex) || {
          question: pendingQuestion?.questions[questionIndex]?.question,
          selected_options: [],
        };
        newAnswers.set(questionIndex, {
          ...current,
          custom_input: value || undefined,
        });
        return newAnswers;
      });
    },
    [pendingQuestion]
  );

  // 提交回答
  const handleSubmit = useCallback(() => {
    if (!pendingQuestion) return;

    const finalAnswers: UserAnswer[] = pendingQuestion.questions.map(
      (q, index) => {
        const answer = answers.get(index);
        if (answer) {
          return { ...answer, question: answer.question ?? q.question };
        }
        // 默认选择第一个选项
        return {
          question: q.question,
          selected_options: q.options.length > 0 ? [q.options[0].label] : [],
        };
      }
    );

    onSubmit(pendingQuestion.request_id, finalAnswers, pendingQuestion.source);
    setAnswers(new Map());
  }, [pendingQuestion, answers, onSubmit]);

  // 取消/关闭
  const handleCancel = useCallback(() => {
    if (activeSessionId) setPendingQuestion(activeSessionId, null);
    setAnswers(new Map());
  }, [activeSessionId, setPendingQuestion]);

  if (!pendingQuestion) {
    return null;
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* 背景遮罩 */}
      <div
        className="absolute inset-0 bg-black/60"
        onClick={handleCancel}
      />

      {/* 弹窗内容 */}
      <div
        className="relative w-full max-w-lg max-h-[80vh] overflow-hidden rounded-xl animate-rise"
        style={{
          backgroundColor: 'var(--color-surface-card)',
          boxShadow: 'var(--effect-shadow-xl)',
        }}
      >
        {/* 标题栏 */}
        <div
          className="px-6 py-4 flex items-center gap-4"
          style={{
            backgroundColor: 'var(--color-surface-panel-strong)',
            borderBottom: '1px solid var(--color-border-default)',
          }}
        >
          <div
            className="w-10 h-10 rounded-lg flex items-center justify-center flex-shrink-0"
            style={{
              background: 'linear-gradient(135deg, var(--color-action-primary), var(--color-brand-secondary))',
            }}
          >
            <svg
              className="w-5 h-5 text-text-inverse"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M8.228 9c.549-1.165 2.03-2 3.772-2 2.21 0 4 1.343 4 3 0 1.4-1.278 2.575-3.006 2.907-.542.104-.994.54-.994 1.093m0 3h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
          </div>
          <div>
            <h2
              className="text-lg font-semibold"
              style={{ color: 'var(--color-text-strong)' }}
            >
              {t('userQuestion.title')}
            </h2>
            <p
              className="text-sm"
              style={{ color: 'var(--color-text-secondary)' }}
            >
              {t('userQuestion.subtitle')}
            </p>
          </div>
        </div>

        {/* 问题列表 */}
        <div
          className="px-6 py-5 overflow-y-auto"
          style={{
            maxHeight: '50vh',
            backgroundColor: 'var(--color-surface-card)',
          }}
        >
          {pendingQuestion.questions.map((question, qIndex) => (
            <QuestionItem
              key={qIndex}
              question={question}
              questionIndex={qIndex}
              answer={answers.get(qIndex)}
              onOptionSelect={handleOptionSelect}
              onCustomInput={handleCustomInput}
            />
          ))}
        </div>

        {/* 操作按钮 */}
        <div
          className="px-6 py-4 flex justify-end gap-3"
          style={{
            backgroundColor: 'var(--color-surface-panel-strong)',
            borderTop: '1px solid var(--color-border-default)',
          }}
        >
          <button
            onClick={handleCancel}
            className="px-4 py-2 text-sm font-medium rounded-lg "
            style={{
              color: 'var(--color-text-secondary)',
              backgroundColor: 'transparent',
            }}
            onMouseOver={(e) => {
              e.currentTarget.style.backgroundColor = 'var(--color-surface-hover)';
              e.currentTarget.style.color = 'var(--color-text-primary)';
            }}
            onMouseOut={(e) => {
              e.currentTarget.style.backgroundColor = 'transparent';
              e.currentTarget.style.color = 'var(--color-text-secondary)';
            }}
          >
            {t('common.cancel')}
          </button>
          <button
            onClick={handleSubmit}
            className="px-5 py-2 text-sm font-medium text-text-inverse rounded-lg  hover:opacity-90"
            style={{
              background: 'linear-gradient(135deg, var(--color-action-primary), var(--color-brand-secondary))',
            }}
          >
            {t('userQuestion.submit')}
          </button>
        </div>
      </div>
    </div>
  );
}

interface QuestionItemProps {
  question: Question;
  questionIndex: number;
  answer?: UserAnswer;
  onOptionSelect: (
    questionIndex: number,
    optionLabel: string,
    isMultiSelect: boolean
  ) => void;
  onCustomInput: (questionIndex: number, value: string) => void;
}

function QuestionItem({
  question,
  questionIndex,
  answer,
  onOptionSelect,
  onCustomInput,
}: QuestionItemProps) {
  const { t } = useTranslation();
  const [showCustomInput, setShowCustomInput] = useState(false);

  const selectedOptions = answer?.selected_options || [];

  return (
    <div className="mb-6 last:mb-0">
      {/* 问题标题 */}
      <div className="mb-3">
        <span
          className="inline-block px-2 py-0.5 text-xs font-medium rounded mb-2"
          style={{
            color: 'var(--color-action-primary)',
            backgroundColor: 'var(--color-action-primary-subtle)',
          }}
        >
          {question.header}
        </span>
        <p
          className="font-medium"
          style={{ color: 'var(--color-text-strong)' }}
        >
          {question.question}
        </p>
        {question.multi_select && (
          <p
            className="text-xs mt-1"
            style={{ color: 'var(--color-text-secondary)' }}
          >
            {t('userQuestion.multiSelect')}
          </p>
        )}
      </div>

      {/* 选项列表 */}
      <div className="space-y-2">
        {question.options.map((option, oIndex) => {
          const isSelected = selectedOptions.includes(option.label);
          return (
            <button
              key={oIndex}
              onClick={() =>
                onOptionSelect(
                  questionIndex,
                  option.label,
                  question.multi_select || false
                )
              }
              className="w-full text-left px-4 py-3 rounded-lg "
              style={{
                backgroundColor: isSelected
                  ? 'var(--color-action-primary-subtle)'
                  : 'var(--color-surface-elevated)',
                border: `1px solid ${
                  isSelected ? 'var(--color-action-primary)' : 'var(--color-border-default)'
                }`,
                color: isSelected ? 'var(--color-text-strong)' : 'var(--color-text-primary)',
              }}
            >
              <div className="flex items-center gap-3">
                <div
                  className="w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 "
                  style={{
                    border: `2px solid ${
                      isSelected ? 'var(--color-action-primary)' : 'var(--color-border-strong)'
                    }`,
                    backgroundColor: isSelected ? 'var(--color-action-primary)' : 'transparent',
                  }}
                >
                  {isSelected && (
                    <svg
                      className="w-3 h-3 text-text-inverse"
                      fill="currentColor"
                      viewBox="0 0 20 20"
                    >
                      <path
                        fillRule="evenodd"
                        d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                        clipRule="evenodd"
                      />
                    </svg>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="font-medium">{option.label}</div>
                  {option.description && (
                    <div
                      className="text-sm mt-0.5"
                      style={{ color: 'var(--color-text-secondary)' }}
                    >
                      {option.description}
                    </div>
                  )}
                </div>
              </div>
            </button>
          );
        })}

        {/* 自定义输入选项 */}
        <button
          onClick={() => setShowCustomInput(!showCustomInput)}
          className="w-full text-left px-4 py-3 rounded-lg "
          style={{
            backgroundColor: showCustomInput
              ? 'var(--color-action-primary-subtle)'
              : 'var(--color-surface-elevated)',
            border: `1px solid ${
              showCustomInput ? 'var(--color-action-primary)' : 'var(--color-border-default)'
            }`,
            color: showCustomInput ? 'var(--color-text-strong)' : 'var(--color-text-primary)',
          }}
        >
          <div className="flex items-center gap-3">
            <div
              className="w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 "
              style={{
                border: `2px solid ${
                  showCustomInput ? 'var(--color-action-primary)' : 'var(--color-border-strong)'
                }`,
                backgroundColor: showCustomInput ? 'var(--color-action-primary)' : 'transparent',
              }}
            >
              {showCustomInput && (
                <svg
                  className="w-3 h-3 text-text-inverse"
                  fill="currentColor"
                  viewBox="0 0 20 20"
                >
                  <path
                    fillRule="evenodd"
                    d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z"
                    clipRule="evenodd"
                  />
                </svg>
              )}
            </div>
            <div className="font-medium">{t('userQuestion.customOption')}</div>
          </div>
        </button>

        {/* 自定义输入框 */}
        {showCustomInput && (
          <div className="mt-2 ml-8">
            <textarea
              value={answer?.custom_input || ''}
              onChange={(e) => onCustomInput(questionIndex, e.target.value)}
              placeholder={t('userQuestion.customPlaceholder')}
              className="w-full px-3 py-2 text-sm rounded-lg resize-none focus:outline-none"
              style={{
                backgroundColor: 'var(--color-surface-elevated)',
                border: '1px solid var(--color-border-default)',
                color: 'var(--color-text-primary)',
              }}
              onFocus={(e) => {
                e.currentTarget.style.borderColor = 'var(--color-action-primary)';
              }}
              onBlur={(e) => {
                e.currentTarget.style.borderColor = 'var(--color-border-default)';
              }}
              rows={3}
            />
          </div>
        )}
      </div>
    </div>
  );
}
