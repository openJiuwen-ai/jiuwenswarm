/**
 * AuthorizationPrompt — 授权 / 操作确认吸附条
 *
 * 吸附在输入框正上方（不随消息滚动）。复用后端下发的选项
 * （如 本次允许 / 总是允许 / 拒绝），按语义重排按钮顺序，文案原样呈现；
 * 选项值原样回传，不改任何后端行为。确认后不在对话中回显。
 *
 * 选项语义匹配与回答装配在 ./promptRouting：选项取值先按后端词表归一化，
 * 匹配一律先认用户点的那个选项；某道题对不上时禁用按钮，不再拿首个选项顶替。
 *
 * 结构：标题行（后端 header，如「权限审批: write_file」）+ 动作按钮；
 * Reviewer reasons use literal text; other permission bodies retain Markdown.
 * 动作按钮 hover 说明用 portal 挂到 body，避免被容器 overflow 截断。
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ShieldCheck, ChevronDown } from 'lucide-react';
import type { AskUserQuestionPayload, Question, UserAnswer } from '../../types';
import { formatToolArguments } from '../../utils';
import {
  buildAuthorizationAnswers,
  canApplyAuthorizationAction,
  resolveAuthorizationActions,
  type ResolvedAction,
} from './promptRouting';
import { AutoReviewerDetails } from '../ChatPanel/AutoReviewerStatus';
import { permissionQuestionKind } from '../../stores/pendingQuestionQueue';
import { translatePermissionText } from './permissionTextI18n';
import { isEnterprise } from '../../edition';

interface AuthorizationPromptProps {
  pending: AskUserQuestionPayload;
  onSubmit: (requestId: string, answers: UserAnswer[], source?: string) => Promise<boolean>;
}

/** 与旧版 InlineQuestionCard 一致的 Tailwind Typography 类，保证正文渲染观感。 */
const PROSE_CLS =
  'prose prose-sm max-w-none prose-headings:font-semibold prose-headings:text-sm prose-ul:my-1 prose-li:my-0 prose-li:pl-1';

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
  const { t, i18n } = useTranslation();
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
              {translatePermissionText(question.header, i18n.language)}
            </div>
          )}
          <AutoReviewerDetails reviewer={question.reviewer_metadata} />
          {question.reviewer_metadata ? (
            <div className="whitespace-pre-wrap break-words" data-testid="interaction-slot-auth-reason">
              {translatePermissionText(question.question, i18n.language)}
            </div>
          ) : (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {translatePermissionText(question.question, i18n.language)}
            </ReactMarkdown>
          )}
          {question.matched_rule && isEnterprise() && (
            <p
              className="mt-3 text-xs text-text-muted"
              data-testid="interaction-slot-matched-rule"
              data-variant={index}
            >
              <span data-testid="interaction-slot-matched-rule-label">
                {t('authPrompt.matchedRule')}:{' '}
              </span>
              <code className="whitespace-pre-wrap break-words text-text">{question.matched_rule}</code>
            </p>
          )}
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
  const { t, i18n } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const questions = pending.questions ?? [];
  const primary = questions[0];
  const smart = pending.source === 'permission_interrupt' && permissionQuestionKind(questions) === 'smart';

  const isConfirm = pending.source === 'confirm_interrupt';
  const count = questions.length;

  // 按钮的 label/description 原样使用后端下发的文案；semantic 判定用的是选项取值
  // （客户端已按后端词表归一化），排序与语义分类不受写法影响。展示层再叠一层已知
  // 短语的前端翻译（非中文语言下），查不到的原样显示。
  // applicable 为 false 表示这道动作落不到每一张卡上：按钮禁用并给出提示，
  // 而不是拿该卡的首个选项顶替用户点击。
  const actions = useMemo(
    () =>
      resolveAuthorizationActions(questions).map((action) => ({
        ...action,
        label: translatePermissionText(action.label, i18n.language),
        tip: translatePermissionText(action.tip, i18n.language),
        applicable: canApplyAuthorizationAction(questions, action, smart),
      })),
    [questions, smart, i18n.language],
  );

  const handlePick = useCallback(
    async (picked: ResolvedAction) => {
      if (submitting) return;
      // 先装配答案：对不上就不提交，且不能占住 submitting。
      const answers = buildAuthorizationAnswers(questions, picked, smart);
      if (!answers) return;
      setSubmitting(true);
      try {
        await onSubmit(pending.request_id, answers, pending.source);
      } finally {
        setSubmitting(false);
      }
    },
    [submitting, onSubmit, pending, questions, smart],
  );

  if (!primary) return null;

  const fallbackTitle = isConfirm ? t('authPrompt.titleConfirm') : t('authPrompt.title');
  const title = translatePermissionText(primary.header, i18n.language).trim() || fallbackTitle;

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
            <HoverTip
              text={action.applicable ? action.tip : t('authPrompt.unavailable')}
              key={action.semantic + action.option.label}
            >
              <button
                type="button"
                className={`auth-prompt__btn auth-prompt__btn--${action.semantic}`}
                disabled={submitting || !action.applicable}
                aria-disabled={!action.applicable}
                title={action.applicable ? undefined : t('authPrompt.unavailable')}
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
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {translatePermissionText(firstLine(primary.question), i18n.language)}
          </ReactMarkdown>
        )}
      </div>
    </div>
  );
}
