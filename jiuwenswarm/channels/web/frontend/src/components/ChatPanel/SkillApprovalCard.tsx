/**
 * SkillApprovalCard 组件
 *
 * Skill 加载门禁审批卡（技能级动态授权）。契约见
 * `types/websocket.ts` 的 `SkillApprovalCardPayload`（后端 `SkillApprovalCard.to_dict()`，
 * 经 interrupt `payload_schema["x-skill-approval-card"]` 下发）。
 *
 * - 展示 Skill 身份（名称/版本/来源/可信标记/声明摘要）与权限差分
 *   （放宽项高亮在前、收紧项其次、被安全策略丢弃项灰色单列）；
 * - 三动作按钮：本地允许 / 会话内允许（仅 trust=builtin 时渲染）/ 不授权但继续加载；
 * - 命中会话审批缓存（cached_decision）时标注"本会话已批准"；
 * - 未收到结构化卡片（旧通道只透传 message）时回退渲染 message markdown。
 *
 * 回传：沿用现有 permission_interrupt 应答通道（chat.send + answers.selected_options），
 * 动作到线上标签的映射固定为：approve_once→本地允许、
 * approve_session→会话内允许、continue_without_overlay→不授权继续
 * （与后端 `_build_interactive_input_from_answers` 对齐）。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { webRequest } from '../../services/webClient';
import { useChatStore } from '../../stores';
import type { SkillApprovalAction, SkillApprovalCardPayload } from '../../types';
import { shouldUnlockPendingApproval } from '../InteractionSlot/interactionSubmission';
import {
  buildSkillSourceDisplayNameMap,
  resolveSkillSourceDisplayName,
  shouldShowSkillTrustBadge,
  type SkillSourceDisplayNames,
} from './skillApprovalPresentation';

interface SkillApprovalCardProps {
  onSubmit: (
    requestId: string,
    answers: { selected_options: string[]; action?: SkillApprovalAction }[],
    source?: string,
  ) => Promise<void>;
  card: SkillApprovalCardPayload | null;
}

/** 动作 → 既有应答通道的线上标签（服务端按固定中文标签映射回审批动作）。 */
const ACTION_WIRE_LABELS: Record<SkillApprovalAction, string> = {
  approve_once: '本次允许',
  approve_session: '会话内允许',
  continue_without_overlay: '仅加载不授权',
};

const sourceDisplayNameRequests = new Map<string, Promise<SkillSourceDisplayNames>>();

function fetchSkillSourceDisplayNames(sessionId: string): Promise<SkillSourceDisplayNames> {
  const cached = sourceDisplayNameRequests.get(sessionId);
  if (cached) return cached;

  const request = webRequest<{
    success?: boolean;
    providers?: Array<{ source_id?: unknown; display_name?: unknown }>;
  }>('skills.source.providers', { session_id: sessionId })
    .then(response => buildSkillSourceDisplayNameMap(response.success === false ? [] : response.providers))
    .catch(() => {
      sourceDisplayNameRequests.delete(sessionId);
      return new Map<string, string>();
    });
  sourceDisplayNameRequests.set(sessionId, request);
  return request;
}

/** 来源路径截断：只保留末尾两段，完整路径交给 title 悬停。 */
function truncateSource(source: string): string {
  const parts = source.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 2) return source;
  return `.../${parts.slice(-2).join('/')}`;
}

export function isValidSkillApprovalCard(raw: unknown): raw is SkillApprovalCardPayload {
  if (!raw || typeof raw !== 'object') return false;
  const card = raw as Partial<SkillApprovalCardPayload>;
  const allowedActions: SkillApprovalAction[] = ['approve_once', 'approve_session', 'continue_without_overlay'];
  return (
    card.kind === 'skill_approval' &&
    card.schema_version === 1 &&
    typeof card.skill_name === 'string' &&
    typeof card.source === 'string' &&
    (card.trust === 'builtin' || card.trust === 'other') &&
    typeof card.permissions_hash === 'string' &&
    typeof card.agent_scope_id === 'string' &&
    Array.isArray(card.actions) &&
    card.actions.length > 0 &&
    card.actions.every(action => allowedActions.includes(action)) &&
    !!card.diff &&
    Array.isArray(card.diff.widened) &&
    card.diff.widened.every(item => typeof item === 'string') &&
    Array.isArray(card.diff.tightened) &&
    card.diff.tightened.every(item => typeof item === 'string') &&
    Array.isArray(card.diff.rejected) &&
    card.diff.rejected.every(item => typeof item === 'string')
  );
}

export function SkillApprovalCard({ onSubmit, card }: SkillApprovalCardProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore(s => s.activeSessionId);
  const pendingQuestion = useChatStore(s => s.runtimes[activeSessionId ?? '']?.pendingQuestion ?? null);
  const [submitted, setSubmitted] = useState(false);
  const [sourceDisplayName, setSourceDisplayName] = useState(card?.source ?? '');

  useEffect(() => {
    const sourceId = card?.source.trim() ?? '';
    setSourceDisplayName(sourceId);
    if (!sourceId || !activeSessionId || card?.trust === 'builtin') return undefined;

    let active = true;
    void fetchSkillSourceDisplayNames(activeSessionId).then(displayNames => {
      if (active) setSourceDisplayName(resolveSkillSourceDisplayName(sourceId, displayNames));
    });
    return () => {
      active = false;
    };
  }, [activeSessionId, card?.source, card?.trust]);

  const question = pendingQuestion?.questions[0];
  const availableActions = useMemo((): SkillApprovalAction[] => {
    if (card) return card.actions;
    const text = question?.question ?? '';
    const actions: SkillApprovalAction[] = ['approve_once'];
    // 后端仅在内置 Skill 的消息中包含 approve_session 说明
    if (text.includes('approve_session')) actions.push('approve_session');
    actions.push('continue_without_overlay');
    return actions;
  }, [card, question]);

  const handleAction = useCallback(
    async (action: SkillApprovalAction) => {
      if (!pendingQuestion || submitted) return;
      const requestId = pendingQuestion.request_id;
      const sessionId = activeSessionId;
      setSubmitted(true);
      await onSubmit(
        requestId,
        [{ selected_options: [ACTION_WIRE_LABELS[action]], action }],
        pendingQuestion.source,
      );
      const state = useChatStore.getState();
      const currentRequestId = sessionId
        ? state.getRuntime(sessionId)?.pendingQuestion?.request_id
        : null;
      if (shouldUnlockPendingApproval(currentRequestId, requestId)) {
        setSubmitted(false);
      }
    },
    [activeSessionId, pendingQuestion, submitted, onSubmit],
  );

  if (!pendingQuestion) {
    return null;
  }

  const showTrustBadge = shouldShowSkillTrustBadge(card?.trust);
  const versionLabel = card?.version || t('chatUi.skillApproval.versionLocal');
  const cachedDecisionLabel = card?.cached_decision === 'session' ? t('chatUi.skillApproval.cachedSession') : t('chatUi.skillApproval.cachedLocal');

  const actionMeta: Record<SkillApprovalAction, { label: string; desc: string; tone: 'ok' | 'accent' | 'muted' }> = {
    approve_once: {
      label: t('chatUi.skillApproval.approveOnce'),
      desc: t('chatUi.skillApproval.approveOnceDesc'),
      tone: 'ok',
    },
    approve_session: {
      label: t('chatUi.skillApproval.approveSession'),
      desc: t('chatUi.skillApproval.approveSessionDesc'),
      tone: 'accent',
    },
    continue_without_overlay: {
      label: t('chatUi.skillApproval.continueWithout'),
      desc: t('chatUi.skillApproval.continueWithoutDesc'),
      tone: 'muted',
    },
  };

  const toneStyle = (tone: 'ok' | 'accent' | 'muted') =>
    tone === 'ok'
      ? { color: 'var(--color-feedback-success)', border: '1px solid var(--color-feedback-success)', background: 'var(--color-feedback-success-subtle)' }
      : tone === 'accent'
        ? { color: 'var(--color-action-primary)', border: '1px solid var(--color-action-primary)', background: 'var(--color-action-primary-subtle)' }
        : { color: 'var(--color-text-primary)', border: '1px solid var(--color-border-default)', background: 'var(--color-surface-elevated)' };

  return (
    <div className="animate-rise mx-2 my-3">
      <div
        className="w-full rounded-xl overflow-hidden"
        style={{ border: '1px solid var(--color-action-primary)', backgroundColor: 'var(--color-surface-card)' }}
      >
        {/* 标题行 */}
        <div
          className="px-4 py-2.5 flex items-center gap-2"
          style={{ borderBottom: '1px solid var(--color-border-default)', backgroundColor: 'var(--color-surface-panel-strong)' }}
        >
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
              d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z"
            />
          </svg>
          <span className="text-xs font-semibold" style={{ color: 'var(--color-action-primary)' }}>
            {t('chatUi.skillApproval.title')}
            {card ? `：${card.skill_name}` : ''}
          </span>
          {card && showTrustBadge && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded"
              style={{
                color: 'var(--color-feedback-success)',
                border: '1px solid var(--color-feedback-success)',
              }}
            >
              {t('chatUi.skillApproval.trustBuiltin')}
            </span>
          )}
        </div>

        <div className="px-4 pt-3 pb-2 text-sm" style={{ color: 'var(--color-text-primary)' }}>
          {card ? (
            <>
              {/* 身份信息 */}
              <div className="text-xs leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>
                <div title={sourceDisplayName === card.source ? card.source : `${sourceDisplayName} (${card.source})`}>
                  {t('chatUi.skillApproval.source')}：<code>{truncateSource(sourceDisplayName || card.source)}</code>　{t('chatUi.skillApproval.version')}：
                  {versionLabel}
                </div>
                <div className="text-[10px]" style={{ color: 'var(--color-text-secondary)', opacity: 0.75 }}>
                  {t('chatUi.skillApproval.hash')}：<code>{card.permissions_hash.slice(0, 12)}</code>
                </div>
              </div>

              {card.cached_decision && (
                <div className="mt-2 text-xs" style={{ color: 'var(--color-action-primary)' }}>
                  {t('chatUi.skillApproval.cached', { decision: cachedDecisionLabel })}
                </div>
              )}

              {/* 权限差分 */}
              {card.diff.widened.length > 0 && (
                <div className="mt-2">
                  <div className="text-xs font-semibold" style={{ color: 'var(--color-action-primary)' }}>
                    {t('chatUi.skillApproval.widened')}
                  </div>
                  <ul className="list-disc pl-5 text-xs leading-relaxed">
                    {card.diff.widened.map((item, idx) => (
                      <li key={idx} style={{ color: 'var(--color-action-primary)' }}>
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {card.diff.tightened.length > 0 && (
                <div className="mt-2" style={{ opacity: 0.75 }}>
                  <div className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('chatUi.skillApproval.tightened')}
                  </div>
                  <ul className="list-disc pl-5 text-[11px] leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>
                    {card.diff.tightened.map((item, idx) => (
                      <li key={idx}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
              {card.diff.rejected.length > 0 && (
                <div className="mt-2" style={{ opacity: 0.65 }}>
                  <div className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>
                    {t('chatUi.skillApproval.rejected')}
                  </div>
                  <ul className="list-disc pl-5 text-[11px] leading-relaxed" style={{ color: 'var(--color-text-secondary)' }}>
                    {card.diff.rejected.map((item, idx) => (
                      <li key={idx}>{item}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          ) : (
            /* 回退：渲染后端下发的 markdown 版卡片 */
            <div className="prose prose-sm max-w-none prose-headings:font-semibold prose-headings:text-sm prose-ul:my-1 prose-li:my-0 prose-li:pl-1">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{question?.question ?? ''}</ReactMarkdown>
            </div>
          )}
        </div>

        {/* 动作按钮 */}
        <div className="px-4 pb-3 flex flex-col gap-2">
          {availableActions.map(action => {
            const meta = actionMeta[action];
            return (
              <button
                key={action}
                onClick={() => handleAction(action)}
                disabled={submitted}
                className="w-full text-left px-4 py-2.5 text-sm font-medium rounded-lg transition-all hover:opacity-85"
                style={{ ...toneStyle(meta.tone), opacity: submitted ? 0.6 : 1 }}
              >
                <div className="flex items-center gap-2">
                  <span>{meta.label}</span>
                  <span className="text-xs font-normal" style={{ color: 'var(--color-text-secondary)' }}>
                    {meta.desc}
                  </span>
                </div>
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
