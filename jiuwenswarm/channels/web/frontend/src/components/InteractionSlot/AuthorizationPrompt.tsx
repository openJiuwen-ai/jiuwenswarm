/**
 * AuthorizationPrompt — 授权 / 操作确认吸附条
 *
 * 吸附在输入框正上方（不随消息滚动）。复用后端下发的选项
 * （如 本次允许 / 总是允许 / 拒绝），仅重排布局，文案原样呈现；
 * 语义与选项值原样回传，不改任何后端行为。确认后不在对话中回显。
 *
 * 结构：标题行（后端 header，如「权限审批: write_file」）+ 动作按钮；
 * 下方正文用 markdown 渲染，收起时渲染首行、展开时渲染完整内容。
 * 动作按钮 hover 说明用 portal 挂到 body，避免被容器 overflow 截断。
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ShieldCheck, ChevronDown } from 'lucide-react';
import type { AskUserQuestionPayload, Question, QuestionOption, UserAnswer } from '../../types';
import { formatToolArguments } from '../../utils';
import { classifyAuthOption, type AuthSemantic } from './promptRouting';
import { AutoReviewerDetails, AutoReviewerStatusBadge } from '../ChatPanel/AutoReviewerStatus';

interface AuthorizationPromptProps {
  pending: AskUserQuestionPayload;
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => Promise<boolean>;
}

/** 动作按钮的显示顺序：跳过(reject) → 永久记住 → 会话内记住 → 授权单次(allow-once)。 */
const ACTION_ORDER: AuthSemantic[] = ['reject', 'allow-always', 'session-allow', 'allow-once'];

/** 与旧版 InlineQuestionCard 一致的 Tailwind Typography 类，保证正文渲染观感。 */
const PROSE_CLS =
  'prose prose-sm max-w-none prose-headings:font-semibold prose-headings:text-sm prose-ul:my-1 prose-li:my-0 prose-li:pl-1';

export interface ResolvedAction {
  semantic: AuthSemantic;
  option: QuestionOption;
  label: string;
  tip: string;
}

function optionSemantic(option: QuestionOption): AuthSemantic {
  return classifyAuthOption(option.value || option.label);
}

export function resolveAuthorizationActions(questions: Question[]): ResolvedAction[] {
  const primary = questions[0];
  const resolved = (primary?.options ?? [])
    .map((option) => ({
      semantic: optionSemantic(option),
      option,
      label: option.label,
      tip: (option.description || '').trim(),
    }))
    .filter((action) =>
      questions.length <= 1 ||
      (action.semantic !== 'other' &&
        questions.every((question) =>
          question.options.some((option) => optionSemantic(option) === action.semantic),
        )),
    );
  const rank = (semantic: AuthSemantic) => {
    const index = ACTION_ORDER.indexOf(semantic);
    return index === -1 ? ACTION_ORDER.length : index;
  };
  return resolved.sort((left, right) => rank(left.semantic) - rank(right.semantic));
}

export function buildAuthorizationAnswers(
  questions: Question[],
  picked: ResolvedAction,
): UserAnswer[] {
  return questions.map((question) => {
    const match =
      question.options.find((option) => optionSemantic(option) === picked.semantic) ||
      question.options.find(
        (option) =>
          (option.value || option.label) === (picked.option.value || picked.option.label),
      );
    const reject = question.options.find((option) => optionSemantic(option) === 'reject');
    const selected = match || reject;
    return { selected_options: [selected ? selected.value || selected.label : 'reject'] };
  });
}

/** 首个非空行，用于收起态渲染。 */
function firstLine(text: string): string {
  return (text || '').split('\n').map((l) => l.trim()).find(Boolean) ?? '';
}

export function formatPermissionPayload(payload: unknown): string {
  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    return formatToolArguments(payload as Record<string, unknown>);
  }
  try {
    return JSON.stringify(payload, null, 2) ?? String(payload);
  } catch {
    return String(payload);
  }
}

export function AuthorizationQuestionDetails({
  questions,
  requestId,
}: {
  questions: Question[];
  requestId: string;
}) {
  const { t } = useTranslation();
  const count = questions.length;
  return (
    <>
      {questions.map((question, index) => (
        <div
          className="auth-prompt__body-item"
          data-testid={`authorization-question-${index}`}
          data-variant={index}
          key={question.card_id || `${requestId}-${index}`}
        >
          {count > 1 && question.header && (
            <div
              className="auth-prompt__body-header"
              data-testid="interaction-slot-auth-body-item-header"
            >
              {question.header}
            </div>
          )}
          <AutoReviewerDetails reviewer={question.reviewer_metadata} />
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{question.question}</ReactMarkdown>
          {question.tool_payload !== undefined && (
            <details
              className="mt-3 rounded-lg border border-border bg-card p-3 text-xs"
              data-testid={`permission-tool-payload-${index}`}
            >
              <summary className="cursor-pointer font-semibold text-text">
                {t('authPrompt.toolPayload.title')}
              </summary>
              <div className="mt-2 text-text-muted">
                {t('authPrompt.toolPayload.notice')}
              </div>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words text-text">
                {formatPermissionPayload(question.tool_payload)}
              </pre>
            </details>
          )}
        </div>
      ))}
    </>
  );
}

/** hover 说明气泡：portal 到 body，始终最上层、不被容器截断。 */
function HoverTip({ text, children }: { text: string; children: React.ReactNode }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  const show = useCallback(() => {
    const rect = wrapRef.current?.getBoundingClientRect();
    if (rect) setPos({ left: rect.left + rect.width / 2, top: rect.top - 8 });
  }, []);
  const hide = useCallback(() => setPos(null), []);

  return (
    <div
      className="auth-prompt__action-wrap"
      ref={wrapRef}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      {children}
      {text && pos &&
        createPortal(
          <span
            className="auth-tip-portal"
            role="tooltip"
            style={{ left: pos.left, top: pos.top }}
            data-testid="interaction-slot-auth-action-tip"
          >
            {text}
          </span>,
          document.body,
        )}
    </div>
  );
}

export function AuthorizationPrompt({ pending, onSubmit }: AuthorizationPromptProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const questions = pending.questions ?? [];
  const primary = questions[0];
  const reviewer = primary?.reviewer_metadata;
  const isConfirm = pending.source === 'confirm_interrupt';
  const count = questions.length;

  // 按钮文案与说明原样使用后端下发的 label / description；
  // semantic 仅用于固定排序与样式映射，不再覆盖显示文案。
  const actions = useMemo<ResolvedAction[]>(
    () => resolveAuthorizationActions(questions),
    [questions],
  );

  /** 把选中的语义应用到所有 question（多条时统一处理）。 */
  const buildAnswers = useCallback(
    (picked: ResolvedAction): UserAnswer[] => buildAuthorizationAnswers(questions, picked),
    [questions],
  );

  const handlePick = useCallback(
    async (picked: ResolvedAction) => {
      if (submitting) return;
      setSubmitting(true);
      await onSubmit(pending.request_id, buildAnswers(picked), pending.source);
      setSubmitting(false);
    },
    [submitting, onSubmit, pending, buildAnswers],
  );

  if (!primary) return null;

  const fallbackTitle = isConfirm ? t('authPrompt.titleConfirm') : t('authPrompt.title');
  const title = (primary.header || '').trim() || fallbackTitle;

  return (
    <div
      className="auth-prompt"
      role="alertdialog"
      aria-label={title}
      data-testid="interaction-slot-auth-prompt"
      data-request-id={pending.request_id}
      data-card-ids={questions
        .map(question => question.card_id)
        .filter(Boolean)
        .join(',')}
    >
      <div
        className="auth-prompt__bar"
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
        data-testid="interaction-slot-auth-bar"
        onClick={() => setExpanded((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            setExpanded((v) => !v);
          }
        }}
      >
        <div className="auth-prompt__head" data-testid="interaction-slot-auth-head">
          <ShieldCheck
            className="auth-prompt__icon"
            size={15}
            strokeWidth={2}
            data-testid="interaction-slot-auth-icon"
          />
          <span
            className="auth-prompt__title"
            title={title}
            data-testid="interaction-slot-auth-title"
          >
            {title}
          </span>
          {count > 1 && (
            <span className="auth-prompt__count" data-testid="interaction-slot-auth-count">
              ({count})
            </span>
          )}
          {count === 1 && <AutoReviewerStatusBadge reviewer={reviewer} />}
          <ChevronDown
            className={`auth-prompt__chevron${expanded ? ' auth-prompt__chevron--open' : ''}`}
            size={14}
            strokeWidth={2}
            data-testid="interaction-slot-auth-chevron"
          />
        </div>

        {/* 动作按钮区不触发展开/收起 */}
        <div className="auth-prompt__actions" onClick={(e) => e.stopPropagation()} data-testid="interaction-slot-auth-actions">
          {actions.map((action) => (
            <HoverTip text={action.tip} key={action.semantic + action.option.label}>
              <button
                type="button"
                className={`auth-prompt__btn auth-prompt__btn--${action.semantic}`}
                disabled={submitting}
                onClick={() => handlePick(action)}
                data-testid="interaction-slot-auth-action-button"
                data-variant={action.semantic}
              >
                {action.label}
              </button>
            </HoverTip>
          ))}
        </div>
      </div>

      <div
        className={
          expanded
            ? `auth-prompt__body ${PROSE_CLS}`
            : 'auth-prompt__body auth-prompt__body--collapsed'
        }
        style={{ color: 'var(--color-text-primary)' }}
        data-testid="interaction-slot-auth-body"
        data-variant={expanded ? 'expanded' : 'collapsed'}
      >
        {expanded ? (
          <AuthorizationQuestionDetails questions={questions} requestId={pending.request_id} />
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{firstLine(primary.question)}</ReactMarkdown>
        )}
      </div>
    </div>
  );
}
