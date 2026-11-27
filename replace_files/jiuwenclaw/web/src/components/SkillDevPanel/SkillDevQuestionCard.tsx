/**
 * SkillDevQuestionCard - SkillDev 问题澄清内联卡片
 *
 * 在聊天流内以内联卡片形式展示问题澄清请求，
 * 替代全屏弹窗。
 *
 * 功能：
 * - 展示多个问题，每个问题有选项和自定义输入
 * - 支持多选选项
 * - 自定义输入框（当 allow_custom 为 true 时）
 * - 统一提交按钮
 */

import { useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import type { ClarifyQuestion, ClarifyAnswer } from '../../types/skilldev';

interface SkillDevQuestionCardProps {
  questions: ClarifyQuestion[];
  onSubmit: (answers: ClarifyAnswer[], questions: ClarifyQuestion[]) => void;
  disabled?: boolean;
}

export function SkillDevQuestionCard({
  questions,
  onSubmit,
  disabled = false,
}: SkillDevQuestionCardProps) {
  const { t } = useTranslation();

  // 存储每个问题的答案：{ question_id: { selected: string[], custom: string } }
  const [answers, setAnswers] = useState<
    Record<string, { selected: string[]; custom: string }>
  >({});

  const handleOptionToggle = useCallback((questionId: string, optionId: string) => {
    setAnswers((prev) => {
      const current = prev[questionId] || { selected: [], custom: '' };
      const isSelected = current.selected.includes(optionId);
      return {
        ...prev,
        [questionId]: {
          ...current,
          selected: isSelected
            ? current.selected.filter((id) => id !== optionId)
            : [...current.selected, optionId],
        },
      };
    });
  }, []);

  const handleCustomInput = useCallback((questionId: string, value: string) => {
    setAnswers((prev) => ({
      ...prev,
      [questionId]: {
        ...(prev[questionId] || { selected: [] }),
        custom: value,
      },
    }));
  }, []);

  const isQuestionAnswered = useCallback(
    (question: ClarifyQuestion) => {
      const answer = answers[question.id];
      if (!answer) return false;
      return answer.selected.length > 0 || (question.allow_custom && answer.custom?.trim());
    },
    [answers]
  );

  const allQuestionsAnswered = questions.every(isQuestionAnswered);

  const handleSubmit = useCallback(() => {
    if (!allQuestionsAnswered || disabled) return;

    // 构建后端期望的答案格式：将选项标签和自定义输入合并为 answer 字符串
    const formattedAnswers: ClarifyAnswer[] = questions.map((q) => {
      const ans = answers[q.id] || { selected: [], custom: '' };

      // 收集选中的选项标签
      const selectedLabels = ans.selected
        .map((optId) => q.options.find((o) => o.id === optId)?.label)
        .filter(Boolean) as string[];

      // 合并选项和自定义输入
      const parts: string[] = [...selectedLabels];
      if (q.allow_custom && ans.custom?.trim()) {
        parts.push(ans.custom.trim());
      }

      return {
        question_id: q.id,
        answer: parts.join('；') || '无回答',
      };
    });

    onSubmit(formattedAnswers, questions);
  }, [answers, questions, allQuestionsAnswered, disabled, onSubmit]);

  return (
    <div className="animate-rise mx-2 my-3">
      <div
        className="w-full rounded-xl overflow-hidden"
        style={{
          border: '1px solid var(--accent)',
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
              className="w-4 h-4 flex-shrink-0"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              strokeWidth={2}
              style={{ color: 'var(--accent)' }}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9 5.25h.008v.008H12v-.008z"
              />
            </svg>
            <span className="text-sm font-semibold" style={{ color: 'var(--accent)' }}>
              {t('skilldev.clarify.title', '请回答以下问题')}
            </span>
            <span className="text-xs" style={{ color: 'var(--muted)' }}>
              {t('skilldev.clarify.progress', { answered: questions.filter((q) => isQuestionAnswered(q)).length, total: questions.length })}
            </span>
          </div>
        </div>

        {/* 问题列表 */}
        <div className="overflow-y-auto" style={{ maxHeight: '60vh' }}>
          {questions.map((q, idx) => {
            const answer = answers[q.id] || { selected: [], custom: '' };
            const answered = isQuestionAnswered(q);

            return (
              <div
                key={q.id}
                className="px-4 py-4"
                style={idx > 0 ? { borderTop: '1px solid var(--border)' } : undefined}
              >
                {/* 问题标题 */}
                <div className="flex items-start gap-2 mb-3">
                  <span
                    className={`shrink-0 w-6 h-6 rounded-full text-sm font-medium flex items-center justify-center ${
                      answered
                        ? 'bg-ok/10 text-ok'
                        : 'bg-accent/10 text-accent'
                    }`}
                  >
                    {idx + 1}
                  </span>
                  <h3 className="text-sm font-medium text-text pt-0.5">{q.question}</h3>
                </div>

                {/* 选项 */}
                <div className="pl-8 space-y-2">
                  {q.options.map((opt) => {
                    const isSelected = answer.selected.includes(opt.id);
                    return (
                      <button
                        key={opt.id}
                        onClick={() => handleOptionToggle(q.id, opt.id)}
                        disabled={disabled}
                        className={`w-full text-left px-4 py-2.5 rounded-lg border text-sm transition-all ${
                          isSelected
                            ? 'bg-accent/10 border-accent text-accent'
                            : 'bg-bg-elevated border-border text-text hover:border-accent/50'
                        } ${disabled ? 'opacity-60 cursor-not-allowed' : ''}`}
                      >
                        <div className="flex items-center gap-3">
                          <div
                            className={`w-4 h-4 rounded border flex items-center justify-center transition-colors ${
                              isSelected ? 'bg-accent border-accent' : 'border-text-muted'
                            }`}
                          >
                            {isSelected && (
                              <svg
                                className="w-3 h-3 text-white"
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
                          <span>{opt.label}</span>
                        </div>
                      </button>
                    );
                  })}

                  {/* 自定义输入 */}
                  {q.allow_custom && (
                    <div className="pt-2">
                      <label className="text-xs font-medium text-text-muted uppercase tracking-wide">
                        {t('skilldev.clarify.customInput', '其他（请填写）')}
                      </label>
                      <input
                        type="text"
                        value={answer.custom}
                        onChange={(e) => handleCustomInput(q.id, e.target.value)}
                        disabled={disabled}
                        placeholder={t('skilldev.clarify.customPlaceholder', '请输入您的回答...')}
                        className="w-full mt-1.5 bg-secondary border border-border rounded-lg px-3 py-2 text-sm text-text focus:outline-none focus:border-accent disabled:opacity-60"
                      />
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* 底部操作栏 */}
        <div
          className="px-4 py-3 flex items-center justify-between"
          style={{
            borderTop: '1px solid var(--border)',
            backgroundColor: 'var(--panel-strong)',
          }}
        >
          <span className="text-xs" style={{ color: 'var(--muted)' }}>
            {allQuestionsAnswered
              ? t('skilldev.clarify.ready', '已回答全部问题，可以提交')
              : t('skilldev.clarify.incomplete', '请回答所有问题后再提交')}
          </span>
          <button
            onClick={handleSubmit}
            disabled={!allQuestionsAnswered || disabled}
            className="px-5 py-2 text-sm font-medium text-white rounded-lg transition-all"
            style={{
              background: allQuestionsAnswered
                ? 'linear-gradient(135deg, var(--accent), var(--accent-2))'
                : 'var(--border)',
              opacity: allQuestionsAnswered && !disabled ? 1 : 0.5,
              cursor: allQuestionsAnswered && !disabled ? 'pointer' : 'not-allowed',
            }}
          >
            {t('skilldev.clarify.submit', '提交回答')}
          </button>
        </div>
      </div>
    </div>
  );
}
