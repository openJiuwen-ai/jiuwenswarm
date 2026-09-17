/**
 * 对话内交互弹窗的分流逻辑。
 *
 * 后端通过 `chat.ask_user_question` 下发的 pendingQuestion，按 source 分为三类：
 *  - authorization: 工具权限 / 操作确认 / 扩展激活 / 技能演进审批(evolution_interrupt)
 *                   —— 输入框上方吸附「授权条」
 *  - interaction:   Agent 主动提问（ask_user）—— 输入框上方吸附「交互卡」，支持单/多选/输入/多轮
 *  - legacy:        计划审批(plan approval) / 演进审批的旧 source 别名(skill_evolution_approval)
 *                   —— 仍走原 InlineQuestionCard
 */

import type { AskUserQuestionPayload } from '../../types';

export type PromptKind = 'authorization' | 'interaction' | 'legacy' | 'none';

const AUTHORIZATION_SOURCES = new Set([
  'permission_interrupt',
  'confirm_interrupt',
  'activate_confirm',
]);

/** 演进审批：source 或 request_id 前缀识别。 */
export function isEvolutionPrompt(pq: AskUserQuestionPayload | null | undefined): boolean {
  if (!pq) return false;
  if (pq.source === 'evolution_interrupt' || pq.source === 'skill_evolution_approval') {
    return true;
  }
  const rid = pq.request_id ?? '';
  return rid.startsWith('skill_evolve_') || rid.startsWith('team_skill_evolve_');
}

/** 计划审批（exit_plan_mode）：内容较重，保留原大卡片。 */
export function isPlanApprovalPrompt(pq: AskUserQuestionPayload | null | undefined): boolean {
  return !!pq && pq.planApprovalKind === 'plan_approval';
}

export function classifyPrompt(pq: AskUserQuestionPayload | null | undefined): PromptKind {
  if (!pq) return 'none';
  // 技能演进审批（evolution_interrupt）协议已冻结为与 permission_interrupt 一致的
  // allow_once/allow_always/reject 三选一，改走 AuthorizationPrompt；legacy source
  // 别名 skill_evolution_approval（自动接受场景，基本不会真正弹出）维持原状不动。
  if (pq.source === 'evolution_interrupt') return 'authorization';
  if (isEvolutionPrompt(pq) || isPlanApprovalPrompt(pq)) return 'legacy';
  if (pq.source === 'ask_user_interrupt') return 'interaction';
  if (pq.source && AUTHORIZATION_SOURCES.has(pq.source)) return 'authorization';
  // 无 source 但带权限式选项时按授权处理；否则按交互处理。
  const firstOptions = pq.questions?.[0]?.options ?? [];
  const looksLikePermission = firstOptions.some((o) =>
    [
      '本次允许', '总是允许', '永久记住', '会话内记住', '拒绝',
      'allow_once', 'always_allow', 'allow_always', 'session_allow', 'reject',
    ].includes(
      (o.value || o.label || '').trim(),
    ),
  );
  return looksLikePermission ? 'authorization' : 'interaction';
}

// ── 授权选项语义识别 ────────────────────────────────────────────

export type AuthSemantic = 'allow-once' | 'allow-always' | 'session-allow' | 'reject' | 'other';

const ALLOW_ONCE_LABELS = new Set([
  '本次允许', '接收', '接受', '激活', '批准', '开始执行',
  'allow_once', 'Allow Once', 'Approve', 'Proceed',
]);
// 'allow_always' 是技能演进审批接口下发的 value（与旧有的 'always_allow' 别名并存）。
// 「永久记住」为权限中断写盘选项；「总是允许」为产品常用别名。
const ALLOW_ALWAYS_LABELS = new Set([
  '总是允许', '永久记住', 'always_allow', 'allow_always', 'Always Allow',
]);
const SESSION_ALLOW_LABELS = new Set([
  '会话内记住', 'session_allow', 'Session Allow',
]);
const REJECT_LABELS = new Set(['拒绝', 'reject', 'Reject']);

export function classifyAuthOption(label: string): AuthSemantic {
  const key = (label || '').trim();
  if (ALLOW_ONCE_LABELS.has(key)) return 'allow-once';
  if (ALLOW_ALWAYS_LABELS.has(key)) return 'allow-always';
  if (SESSION_ALLOW_LABELS.has(key)) return 'session-allow';
  if (REJECT_LABELS.has(key)) return 'reject';
  return 'other';
}
