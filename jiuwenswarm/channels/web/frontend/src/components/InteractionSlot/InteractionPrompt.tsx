/**
 * InteractionPrompt — Agent 主动提问（ask_user）吸附卡
 *
 * 吸附在输入框正上方。支持单选 / 多选 / 自由输入 / 多轮（每题一页，最多 4 页）。
 * 一次性收集所有页答案后提交；确认后前端合成「问题澄清」卡注入对话流。
 */

import { useCallback, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { FileText, ChevronLeft, ChevronRight } from 'lucide-react';
import { useChatStore } from '../../stores';
import type { AskUserQuestionPayload, Message, Question, UserAnswer } from '../../types';
import { buildQaSummaryContent, type QaSummaryData, type QaSummaryItem } from './qaSummary';

/** 后端为「有选项的问题」追加的自定义输入占位项。 */
const CUSTOM_OPTION_LABEL = 'Other';

/** 多轮上限（产品约定：多轮确认最多不超过 4 轮）。 */
const MAX_PAGES = 4;

/**
 * 「跳过」/「取消」时回传给后端的标记文本。
 *
 * 协议里没有独立于 options 之外的「跳过」/「取消」信号——ask_user 的结构化问答
 * 唯一的输出通道就是 selected_options / custom_input 拼成的文本，最终由 LLM
 * 读文本自行判断语义（见 jiuwenswarm/agents/harness/common/rails/ask_user_rail.py
 * StructuredAskUserRail.resolve_interrupt）。这里比照后端既有的同类标记文本
 * （如 interface.py 里硬编码的「用户拒绝」「用户希望继续规划」）不做 i18n，
 * 因为它是喂给 LLM 的内部信号而非界面文案。
 */
const SKIPPED_ANSWER_TEXT = '用户已跳过此题，未选择任何选项。';
const CANCELLED_ANSWER_TEXT = '用户已取消本次问答，未作答。';

interface InteractionPromptProps {
  pending: AskUserQuestionPayload;
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => void;
}

interface PageState {
  selected: string[];
  custom: string;
  /** 单选场景下是否选中「自定义」项 */
  customActive: boolean;
  /**
   * 该页是否因「点击跳过按钮且当前无任何选择/输入」而被显式标记为跳过。
   * 仅用于 answerFor 判断是否要用 SKIPPED_ANSWER_TEXT 替代「默认选第一项」的兜底；
   * 一旦用户在该页重新做出任何选择/输入，会被清除。
   */
  skippedNoSelection?: boolean;
}

function emptyPage(): PageState {
  return { selected: [], custom: '', customActive: false };
}

export function InteractionPrompt({ pending, onSubmit }: InteractionPromptProps) {
  const { t } = useTranslation();
  const setPendingQuestion = useChatStore((s) => s.setPendingQuestion);
  const addMessage = useChatStore((s) => s.addMessage);

  const questions = useMemo<Question[]>(
    () => (pending.questions ?? []).slice(0, MAX_PAGES),
    [pending.questions],
  );
  const total = questions.length;

  const [page, setPage] = useState(0);
  const [reached, setReached] = useState(0);
  const [states, setStates] = useState<Record<number, PageState>>({});
  const [submitting, setSubmitting] = useState(false);

  const current = questions[page];
  const st = states[page] ?? emptyPage();
  const isLast = page >= total - 1;

  const hasCustomOption = useMemo(
    () => (current?.options ?? []).some((o) => o.label === CUSTOM_OPTION_LABEL),
    [current],
  );
  const normalOptions = useMemo(
    () => (current?.options ?? []).filter((o) => o.label !== CUSTOM_OPTION_LABEL),
    [current],
  );
  const isMulti = !!current?.multi_select;
  const isFreeInput = normalOptions.length === 0; // 无选项 → 纯输入题

  const patch = useCallback(
    (updater: (prev: PageState) => PageState) => {
      setStates((prev) => ({ ...prev, [page]: updater(prev[page] ?? emptyPage()) }));
    },
    [page],
  );

  const toggleOption = useCallback(
    (label: string) => {
      if (submitting) return;
      patch((prev) => {
        if (isMulti) {
          const has = prev.selected.includes(label);
          return {
            ...prev,
            selected: has ? prev.selected.filter((v) => v !== label) : [...prev.selected, label],
            skippedNoSelection: false,
          };
        }
        return { ...prev, selected: [label], customActive: false, skippedNoSelection: false };
      });
    },
    [submitting, isMulti, patch],
  );

  const selectCustom = useCallback(() => {
    if (submitting) return;
    patch((prev) => ({ ...prev, selected: [], customActive: true, skippedNoSelection: false }));
  }, [submitting, patch]);

  const setCustomText = useCallback(
    (text: string) => patch((prev) => ({ ...prev, custom: text, skippedNoSelection: false })),
    [patch],
  );

  /** 当前页是否选了 Other 却未填写自定义内容（禁止前进/提交，避免空答触发 thinking）。 */
  const incompleteCustom = st.customActive && !st.custom.trim();

  /** 把某页状态转为一个 UserAnswer。 */
  const answerFor = useCallback(
    (idx: number, overrides?: Record<number, PageState>): UserAnswer => {
      const q = questions[idx];
      const s = overrides?.[idx] ?? states[idx] ?? emptyPage();
      const customText = s.custom.trim();
      // 该页是被「跳过」按钮显式标记为「无选择」的：如实告知后端「已跳过」，
      // 不再套用下面「默认选第一项」的兜底（否则会把跳过误传成某个具体选项）。
      if (s.skippedNoSelection && s.selected.length === 0 && !customText) {
        return { question: q?.question, selected_options: [], custom_input: SKIPPED_ANSWER_TEXT };
      }
      // Other 已激活但未输入：不得回落成第一个普通选项（#2330）。
      if (s.customActive && !customText) {
        return { question: q?.question, selected_options: [], custom_input: '' };
      }
      const answer: UserAnswer = { question: q?.question, selected_options: [...s.selected] };
      if (customText) answer.custom_input = customText;
      // 兜底：既无选择也无输入、且不是显式跳过时，默认第一个普通选项（避免后端拿到空答案）。
      // 该分支对应「点确定/下一步但什么都没选」这种情况，维持原有行为，不在本次改动范围内。
      if (answer.selected_options.length === 0 && !customText) {
        const first = (q?.options ?? []).find((o) => o.label !== CUSTOM_OPTION_LABEL);
        if (first) answer.selected_options = [first.value || first.label];
      }
      return answer;
    },
    [questions, states],
  );

  const buildAnswers = useCallback(
    (overrides?: Record<number, PageState>, forcedTextByIdx?: Record<number, string>): UserAnswer[] =>
      questions.map((q, idx) => {
        const forced = forcedTextByIdx?.[idx];
        if (forced !== undefined) {
          return { question: q.question, selected_options: [], custom_input: forced };
        }
        return answerFor(idx, overrides);
      }),
    [questions, answerFor],
  );

  /** 组装「问题澄清」回显数据（仅展示用户真实作答内容）。 */
  const buildSummary = useCallback((overrides?: Record<number, PageState>): QaSummaryData => {
    const items: QaSummaryItem[] = questions.map((q, idx) => {
      const s = overrides?.[idx] ?? states[idx] ?? emptyPage();
      const customText = s.custom.trim();
      if (s.skippedNoSelection && s.selected.length === 0 && !customText) {
        return { header: q.header, question: q.question, answers: [t('interactionPrompt.skippedSummary')] };
      }
      const answers: string[] = [...s.selected];
      if (customText) answers.push(customText);
      return { header: q.header, question: q.question, answers };
    });
    return { title: t('qaSummary.title'), items };
  }, [questions, states, t]);

  const clearPending = useCallback(() => {
    const sid = useChatStore.getState().activeSessionId;
    if (sid) setPendingQuestion(sid, null);
  }, [setPendingQuestion]);

  const submit = useCallback(
    (withEcho: boolean, overrides?: Record<number, PageState>, forcedTextByIdx?: Record<number, string>) => {
      if (submitting) return;
      setSubmitting(true);
      if (withEcho) {
        const sid = useChatStore.getState().activeSessionId;
        if (sid) {
          const message: Message = {
            id: `qa-summary-${pending.request_id || Date.now()}`,
            role: 'assistant',
            content: buildQaSummaryContent(buildSummary(overrides)),
            timestamp: new Date().toISOString(),
          };
          addMessage(sid, message);
        }
      }
      onSubmit(pending.request_id, buildAnswers(overrides, forcedTextByIdx), pending.source);
      clearPending();
    },
    [submitting, pending, buildSummary, buildAnswers, addMessage, onSubmit, clearPending],
  );

  const goPrev = useCallback(() => setPage((p) => Math.max(0, p - 1)), []);
  const goNextPage = useCallback(() => {
    setPage((p) => {
      const next = Math.min(total - 1, p + 1);
      setReached((r) => Math.max(r, next));
      return next;
    });
  }, [total]);

  const handleNextOrConfirm = useCallback(() => {
    // Other 空输入：留在当前页提示用户填写，勿提交以免进入黄色 thinking（#2330）。
    if (incompleteCustom) return;
    if (isLast) submit(true);
    else goNextPage();
  }, [incompleteCustom, isLast, submit, goNextPage]);

  const handleSkip = useCallback(() => {
    // 跳过：无论当前页此前是否已经选中过某个选项/填过自定义输入，点击"跳过"
    // 都必须视为"这道题被跳过了"，绝不能把之前的选择原样带出去（bug011）。
    // 显式标记为"已跳过"，后续 answerFor/buildSummary 会用 SKIPPED_ANSWER_TEXT
    // 如实告知后端，而不是套用"默认选第一项"的兜底逻辑——这一点是本次修复的核心，
    // 务必确保 skippedNoSelection 分支在 answerFor 里排在"默认选第一项"兜底之前
    // （见 answerFor 实现），不会被兜底逻辑抢先命中。
    // patch() 触发的 setStates 是异步的，若末页直接 submit 会读到旧值，
    // 因此显式构造覆盖态传给 submit，避免依赖尚未生效的 state。
    const skippedState: PageState = { ...emptyPage(), skippedNoSelection: true };
    const overridden = { ...states, [page]: skippedState };
    patch(() => skippedState);
    if (isLast) submit(true, overridden);
    else goNextPage();
  }, [states, page, patch, isLast, submit, goNextPage]);

  const handleCancel = useCallback(() => {
    // 取消整轮：不管当前页/其它页有没有选择、选了什么，统一对每道题回传明确的
    // "已取消"标记，避免后端/LLM 把取消误当成某个具体选项的选择；不回显到聊天记录。
    const forcedTextByIdx: Record<number, string> = {};
    questions.forEach((_, idx) => {
      forcedTextByIdx[idx] = CANCELLED_ANSWER_TEXT;
    });
    submit(false, undefined, forcedTextByIdx);
  }, [submit, questions]);

  if (!current) return null;

  return (
    <div className="ix-prompt" role="dialog" aria-label={t('interactionPrompt.title')} data-testid="interaction-slot-ix-prompt">
      <div className="ix-prompt__head" data-testid="interaction-slot-ix-head">
        <div className="ix-prompt__title" data-testid="interaction-slot-ix-title">
          <FileText size={15} strokeWidth={2} className="ix-prompt__title-icon" data-testid="interaction-slot-ix-title-icon" />
          <span data-testid="interaction-slot-ix-title-text">{current.header || t('interactionPrompt.title')}</span>
        </div>
        {total > 1 && (
          <div className="ix-prompt__pager" data-testid="interaction-slot-ix-pager">
            <button
              type="button"
              className="ix-prompt__pager-btn"
              onClick={goPrev}
              disabled={page === 0}
              aria-label={t('interactionPrompt.prev')}
              data-testid="interaction-slot-ix-pager-prev"
            >
              <ChevronLeft size={16} strokeWidth={2} />
            </button>
            <span className="ix-prompt__pager-label" data-testid="interaction-slot-ix-pager-label">
              {page + 1}/{total}
            </span>
            <button
              type="button"
              className="ix-prompt__pager-btn"
              onClick={goNextPage}
              disabled={page >= reached || page >= total - 1}
              aria-label={t('interactionPrompt.next')}
              data-testid="interaction-slot-ix-pager-next"
            >
              <ChevronRight size={16} strokeWidth={2} />
            </button>
          </div>
        )}
      </div>

      <div className="ix-prompt__body" data-testid="interaction-slot-ix-body">
        <div className="ix-prompt__question chat-text" data-testid="interaction-slot-ix-question">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{current.question}</ReactMarkdown>
        </div>

        <div
          className={`ix-prompt__group${isMulti ? ' ix-prompt__group--multi' : ''}`}
          data-testid="interaction-slot-ix-options"
          data-variant={isMulti ? 'multi' : undefined}
        >
          {normalOptions.map((option) => {
            const value = option.value || option.label;
            const selected = st.selected.includes(value) || st.selected.includes(option.label);
            return (
              <button
                type="button"
                key={option.label}
                className={`ix-option${selected ? ' ix-option--selected' : ''}`}
                onClick={() => toggleOption(value)}
                disabled={submitting}
                data-testid="interaction-slot-ix-option"
                data-variant={option.label}
              >
                <span className={`ix-option__mark ix-option__mark--${isMulti ? 'check' : 'radio'}`} />
                <span className="ix-option__text">
                  <span className="ix-option__label" data-testid="interaction-slot-ix-option-label">{option.label}</span>
                  {option.description && (
                    <span className="ix-option__desc" data-testid="interaction-slot-ix-option-desc">{option.description}</span>
                  )}
                </span>
              </button>
            );
          })}

          {/* 单选：自定义项作为一个可选项 */}
          {hasCustomOption && !isMulti && (
            <button
              type="button"
              className={`ix-option${st.customActive ? ' ix-option--selected' : ''}`}
              onClick={selectCustom}
              disabled={submitting}
              data-testid="interaction-slot-ix-option-custom"
            >
              <span className="ix-option__mark ix-option__mark--radio" />
              <span className="ix-option__text">
                <span className="ix-option__label" data-testid="interaction-slot-ix-option-custom-label">{t('interactionPrompt.customOption')}</span>
              </span>
            </button>
          )}

          {/* 自定义输入框：纯输入题 / 多选题常驻；单选题选中自定义后出现 */}
          {(isFreeInput || (hasCustomOption && (isMulti || st.customActive))) && (
            <textarea
              className="ix-prompt__custom"
              value={st.custom}
              placeholder={t('interactionPrompt.customPlaceholder')}
              onChange={(e) => setCustomText(e.target.value)}
              rows={2}
              disabled={submitting}
              data-testid="interaction-slot-ix-custom-input"
            />
          )}
        </div>
      </div>

      <div className="ix-prompt__foot" data-testid="interaction-slot-ix-foot">
        <button
          type="button"
          className="ix-btn ix-btn--ghost"
          onClick={handleCancel}
          disabled={submitting}
          data-testid="interaction-slot-ix-cancel-button"
        >
          {t('interactionPrompt.cancel')}
        </button>
        <button
          type="button"
          className="ix-btn ix-btn--ghost"
          onClick={handleSkip}
          disabled={submitting}
          data-testid="interaction-slot-ix-skip-button"
        >
          {t('interactionPrompt.skip')}
        </button>
        <button
          type="button"
          className="ix-btn ix-btn--primary"
          onClick={handleNextOrConfirm}
          disabled={submitting || incompleteCustom}
          data-testid="interaction-slot-ix-confirm-button"
          data-variant={isLast ? 'confirm' : 'next'}
        >
          {isLast ? t('interactionPrompt.confirm') : t('interactionPrompt.nextStep')}
        </button>
      </div>
    </div>
  );
}
