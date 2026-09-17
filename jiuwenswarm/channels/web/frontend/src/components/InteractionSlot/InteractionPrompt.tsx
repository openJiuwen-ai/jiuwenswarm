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
import type { AskUserQuestionPayload, Message, Question, UserAnswer, UserAnswerStatus } from '../../types';
import { buildEmptyAskUserAnswers, isIncompleteAskUserPage, resolveAskUserStatus } from './interactionSubmission';
import { buildQaSummaryContent, type QaSummaryData, type QaSummaryItem } from './qaSummary';

/** 后端为「有选项的问题」追加的自定义输入占位项。 */
const CUSTOM_OPTION_LABEL = 'Other';

/** 多轮上限（产品约定：多轮确认最多不超过 4 轮）。 */
const MAX_PAGES = 4;

interface InteractionPromptProps {
  pending: AskUserQuestionPayload;
  onSubmit: (
    requestId: string,
    answers: UserAnswer[],
    source?: string,
    status?: UserAnswerStatus,
  ) => void;
}

interface PageState {
  selected: string[];
  custom: string;
  /** 单选场景下是否选中「自定义」项 */
  customActive: boolean;
  /**
   * 该页是否被用户显式跳过，用于返回空答案并显示跳过摘要；
   * 一旦用户在该页重新做出任何选择/输入，会被清除。
   */
  skippedNoSelection?: boolean;
}

function emptyPage(question?: Question): PageState {
  const first = question?.options?.find(option => option.label !== CUSTOM_OPTION_LABEL);
  return { selected: first ? [first.value || first.label] : [], custom: '', customActive: false };
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
  const st = states[page] ?? emptyPage(current);
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
      setStates((prev) => ({ ...prev, [page]: updater(prev[page] ?? emptyPage(current)) }));
    },
    [page, current],
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

  /** 显式空选或空 Other 不得前进/提交；跳过仍允许空答案。 */
  const incompleteAnswer = current ? isIncompleteAskUserPage(current, st) : false;

  /** 把某页状态转为一个 UserAnswer。 */
  const answerFor = useCallback(
    (idx: number, overrides?: Record<number, PageState>): UserAnswer => {
      const q = questions[idx];
      const s = overrides?.[idx] ?? states[idx] ?? emptyPage(q);
      const customText = s.custom.trim();
      // 该页被显式跳过时返回空答案；交互状态由顶层 status 表达，不能再把
      // 内部提示文本伪装成 custom_input，也不能套用默认第一项的兜底。
      if (s.skippedNoSelection && s.selected.length === 0 && !customText) {
        return { question: q?.question, selected_options: [], custom_input: '' };
      }
      // Other 已激活但未输入：不得回落成第一个普通选项（#2330）。
      if (s.customActive && !customText) {
        return { question: q?.question, selected_options: [], custom_input: '' };
      }
      const answer: UserAnswer = { question: q?.question, selected_options: [...s.selected] };
      if (customText) answer.custom_input = customText;
      return answer;
    },
    [questions, states],
  );

  const buildAnswers = useCallback(
    (overrides?: Record<number, PageState>): UserAnswer[] =>
      questions.map((_q, idx) => answerFor(idx, overrides)),
    [questions, answerFor],
  );

  /** 组装「问题澄清」回显数据（仅展示用户真实作答内容）。 */
  const buildSummary = useCallback((overrides?: Record<number, PageState>): QaSummaryData => {
    const items: QaSummaryItem[] = questions.map((q, idx) => {
      const s = overrides?.[idx] ?? states[idx] ?? emptyPage(q);
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
    (
      withEcho: boolean,
      requestedStatus: UserAnswerStatus,
      overrides?: Record<number, PageState>,
      discardAnswers = false,
    ) => {
      if (submitting) return;
      if (!discardAnswers) {
        const incompletePage = questions.findIndex((q, idx) => {
          const state = overrides?.[idx] ?? states[idx] ?? emptyPage(q);
          return isIncompleteAskUserPage(q, state);
        });
        if (incompletePage !== -1) {
          setPage(incompletePage);
          return;
        }
      }
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
      const answers = discardAnswers
        ? buildEmptyAskUserAnswers(questions)
        : buildAnswers(overrides);
      // “跳过当前页”后仍可能已有其他页的真实回答；此时整轮仍是 answered。
      const status = resolveAskUserStatus(requestedStatus, answers);
      onSubmit(pending.request_id, answers, pending.source, status);
      clearPending();
    },
    [submitting, pending, buildSummary, buildAnswers, questions, states, addMessage, onSubmit, clearPending],
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
    // 空 Other 或多选空答案必须先补充内容（#2330）。
    if (incompleteAnswer) return;
    if (isLast) submit(true, 'answered');
    else goNextPage();
  }, [incompleteAnswer, isLast, submit, goNextPage]);

  const handleSkip = useCallback(() => {
    // 跳过：无论当前页此前是否已经选中过某个选项/填过自定义输入，点击"跳过"
    // 都必须视为"这道题被跳过了"，绝不能把之前的选择原样带出去（bug011）。
    // patch() 触发的 setStates 是异步的，若末页直接 submit 会读到旧值，
    // 因此显式构造覆盖态传给 submit，避免依赖尚未生效的 state。
    const skippedState: PageState = { selected: [], custom: '', customActive: false, skippedNoSelection: true };
    const overridden = { ...states, [page]: skippedState };
    patch(() => skippedState);
    if (isLast) submit(true, 'skipped', overridden);
    else goNextPage();
  }, [states, page, patch, isLast, submit, goNextPage]);

  const handleCancel = useCallback(() => {
    // 当前协议不区分 skip/cancel；取消整轮统一提交 skipped + 空 answers，且不回显。
    submit(false, 'skipped', undefined, true);
  }, [submit]);

  if (!current) return null;

  return (
    <div className="ix-prompt" role="dialog" aria-label={t('interactionPrompt.title')}>
      <div className="ix-prompt__head">
        <div className="ix-prompt__title">
          <FileText size={15} strokeWidth={2} className="ix-prompt__title-icon" />
          <span>{current.header || t('interactionPrompt.title')}</span>
        </div>
        {total > 1 && (
          <div className="ix-prompt__pager">
            <button
              type="button"
              className="ix-prompt__pager-btn"
              onClick={goPrev}
              disabled={page === 0}
              aria-label={t('interactionPrompt.prev')}
            >
              <ChevronLeft size={16} strokeWidth={2} />
            </button>
            <span className="ix-prompt__pager-label">
              {page + 1}/{total}
            </span>
            <button
              type="button"
              className="ix-prompt__pager-btn"
              onClick={goNextPage}
              disabled={incompleteAnswer || page >= reached || page >= total - 1}
              aria-label={t('interactionPrompt.next')}
            >
              <ChevronRight size={16} strokeWidth={2} />
            </button>
          </div>
        )}
      </div>

      <div className="ix-prompt__body">
        <div className="ix-prompt__question chat-text">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{current.question}</ReactMarkdown>
        </div>

        <div className={`ix-prompt__group${isMulti ? ' ix-prompt__group--multi' : ''}`}>
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
              >
                <span className={`ix-option__mark ix-option__mark--${isMulti ? 'check' : 'radio'}`} />
                <span className="ix-option__text">
                  <span className="ix-option__label">{option.label}</span>
                  {option.description && (
                    <span className="ix-option__desc">{option.description}</span>
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
            >
              <span className="ix-option__mark ix-option__mark--radio" />
              <span className="ix-option__text">
                <span className="ix-option__label">{t('interactionPrompt.customOption')}</span>
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
            />
          )}
        </div>
        {incompleteAnswer && (
          <p role="status" className="text-warn">
            {t(st.customActive ? 'interactionPrompt.customRequired' : 'interactionPrompt.selectionRequired')}
          </p>
        )}
      </div>

      <div className="ix-prompt__foot">
        <button
          type="button"
          className="ix-btn ix-btn--ghost"
          onClick={handleCancel}
          disabled={submitting}
        >
          {t('interactionPrompt.cancel')}
        </button>
        <button
          type="button"
          className="ix-btn ix-btn--ghost"
          onClick={handleSkip}
          disabled={submitting}
        >
          {t('interactionPrompt.skip')}
        </button>
        <button
          type="button"
          className="ix-btn ix-btn--primary"
          onClick={handleNextOrConfirm}
          disabled={submitting || incompleteAnswer}
        >
          {isLast ? t('interactionPrompt.confirm') : t('interactionPrompt.nextStep')}
        </button>
      </div>
    </div>
  );
}
