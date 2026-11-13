/**
 * 对话内交互弹窗的分流逻辑与授权选项语义识别。
 *
 * 后端通过 `chat.ask_user_question` 下发的 pendingQuestion，按来源分流：
 *  - authorization: 工具权限 / 操作确认 / 扩展激活 / 技能演进审批(evolution_interrupt)
 *                   —— 输入框上方吸附「授权条」
 *  - interaction:   Agent 主动提问（ask_user）—— 输入框上方吸附「交互卡」，支持单/多选/输入/多轮
 *  - experience:    Symphony 推荐的可复用技能包——复用 InteractionPrompt 交互窗口
 *  - legacy:        计划审批(plan approval) / 演进审批的旧 source 别名(skill_evolution_approval)
 *                   —— 仍走原 InlineQuestionCard
 *
 * 选项取值可能来自三套命名：后端 permission_options 的下划线 id（allow_once）、
 * ACP 的连字符 id（allow-once）、以及中文/英文展示名。归一化规则与后端
 * permission_options.normalize_option_value 一致，任何一套写法都能认出语义；
 * 提交时一律回传后端下发的原始取值，不在这里生成新的拼写。
 */

import type { AskUserQuestionPayload, Question, QuestionOption, UserAnswer } from '../../types';

export type PromptKind = 'authorization' | 'interaction' | 'experience' | 'legacy' | 'none';

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

/** Symphony 运行成功后推荐创建的可复用技能包。 */
export function isSkillPackagePrompt(pq: AskUserQuestionPayload | null | undefined): boolean {
  if (!pq) return false;
  if (pq.evolutionMeta?.rail_kind === 'symphony_experience') return true;
  return (pq.request_id ?? '').startsWith('symphony_experience_');
}

export function classifyPrompt(pq: AskUserQuestionPayload | null | undefined): PromptKind {
  if (!pq) return 'none';
  if (pq.skill_approval_card?.kind === 'skill_approval') return 'legacy';
  if (isSkillPackagePrompt(pq)) return 'experience';
  if (pq.source === 'swarmflow_human') return 'interaction';
  // 技能演进审批（evolution_interrupt）协议已冻结为与 permission_interrupt 一致的
  // allow_once/allow_always/reject 三选一，改走 AuthorizationPrompt；legacy source
  // 别名 skill_evolution_approval（自动接受场景，基本不会真正弹出）维持原状不动。
  if (pq.source === 'evolution_interrupt') return 'authorization';
  if (isEvolutionPrompt(pq) || isPlanApprovalPrompt(pq)) return 'legacy';
  if (pq.source === 'ask_user_interrupt') return 'interaction';
  if (pq.source && AUTHORIZATION_SOURCES.has(pq.source)) return 'authorization';
  // 无 source 但带权限式选项时按授权处理；否则按交互处理。词表与按钮语义共用一份，
  // 因此连字符 / 大小写 / 其他后端别名变体同样能命中（相对旧的裸字面量表是有意放宽）。
  const firstOptions = pq.questions?.[0]?.options ?? [];
  const looksLikePermission = firstOptions.some(
    (option) => classifyAuthOption(option.value || option.label || '') !== 'other',
  );
  return looksLikePermission ? 'authorization' : 'interaction';
}

// ── 授权选项语义识别 ────────────────────────────────────────────

export type AuthSemantic = 'allow-once' | 'allow-always' | 'session-allow' | 'reject' | 'other';

/** 动作按钮的显示顺序：跳过(reject) → 永久记住 → 会话内记住 → 授权单次(allow-once)。 */
export const ACTION_ORDER: AuthSemantic[] = ['reject', 'allow-always', 'session-allow', 'allow-once'];

/** 与后端 permission_options.normalize_option_value 同一规则：空白 / 下划线 / 连字符等价，忽略大小写。 */
const OPTION_SEPARATORS = /[\s_-]+/g;

export function normalizeAuthOption(value: string): string {
  const text = (value || '').trim();
  return text ? text.replace(OPTION_SEPARATORS, '_').toLowerCase() : '';
}

// 词表与后端 permission_options._ALIAS_SOURCES（permission / confirm 路径）对齐。
// 键写成 normalizeAuthOption 之后的形式，因此 'Allow Once'、'allow-once'、'ALLOW_ONCE'
// 都落进同一个集合。计划审批的 legacy 取值（继续规划 / 其他意见 / keep_planning）
// 不在此列：它们只出现在 InlineQuestionCard 的旧卡上。
const ALLOW_ONCE_LABELS = new Set([
  '本次允许', '接收', '接受', '激活', '批准', '开始执行',
  'allow_once', 'approve', 'accept', 'proceed',
]);
// 'allow_always' 是技能演进审批接口下发的 value（与旧有的 'always_allow' 别名并存）。
// 「永久记住」为权限中断写盘选项；「总是允许」为产品常用别名。
const ALLOW_ALWAYS_LABELS = new Set([
  '总是允许', '永久记住', 'always_allow', 'allow_always',
]);
const SESSION_ALLOW_LABELS = new Set([
  '会话内记住', 'session_allow',
]);
const REJECT_LABELS = new Set(['拒绝', 'reject', 'reject_once']);

export function classifyAuthOption(label: string): AuthSemantic {
  const key = normalizeAuthOption(label);
  if (!key) return 'other';
  if (ALLOW_ONCE_LABELS.has(key)) return 'allow-once';
  if (ALLOW_ALWAYS_LABELS.has(key)) return 'allow-always';
  if (SESSION_ALLOW_LABELS.has(key)) return 'session-allow';
  if (REJECT_LABELS.has(key)) return 'reject';
  return 'other';
}

// ── 授权动作与回答装配 ──────────────────────────────────────────

export interface ResolvedAction {
  semantic: AuthSemantic;
  option: QuestionOption;
  label: string;
  tip: string;
}

/** 选项的协议取值：`value` 可选（activate_confirm 卡片只给 label），回传时统一用它。 */
function optionWireValue(option: QuestionOption): string {
  return (option.value || option.label || '').trim();
}

export function resolveAuthorizationActions(questions: Question[]): ResolvedAction[] {
  const primary = questions[0];
  const resolved = (primary?.options ?? []).map((option) => ({
    semantic: classifyAuthOption(optionWireValue(option)),
    option,
    label: option.label,
    tip: (option.description || '').trim(),
  }));
  const rank = (semantic: AuthSemantic) => {
    const index = ACTION_ORDER.indexOf(semantic);
    return index === -1 ? ACTION_ORDER.length : index;
  };
  return resolved.sort((left, right) => rank(left.semantic) - rank(right.semantic));
}

/**
 * 在一道题里找用户点的那个选项：先按协议取值认自己，再按语义认同义项。
 *
 * - 取值相等不区分 `value` 与 `label`：只有 label 的卡片（activate_confirm）也能对得上，
 *   否则 `undefined === undefined` 会让每题都命中首项。
 * - `other` 语义不参与匹配：同题两个未识别选项不能互相顶替。
 */
function findAnswerOption(question: Question, picked: ResolvedAction): QuestionOption | undefined {
  const target = optionWireValue(picked.option);
  if (target) {
    const same = question.options.find((option) => optionWireValue(option) === target);
    if (same) return same;
  }
  if (picked.semantic === 'other') return undefined;
  return question.options.find(
    (option) => classifyAuthOption(optionWireValue(option)) === picked.semantic,
  );
}

function findRejectOption(question: Question): QuestionOption | undefined {
  return question.options.find(
    (option) => classifyAuthOption(optionWireValue(option)) === 'reject',
  );
}

/** 该动作能否落到每一道题上；smart（单卡自动审阅）恒为真，走拒绝兜底。 */
export function canApplyAuthorizationAction(
  questions: Question[],
  picked: ResolvedAction,
  smart = false,
): boolean {
  if (smart) return true;
  return questions.every((question) => findAnswerOption(question, picked) !== undefined);
}

/**
 * 把选中的动作应用到所有 question（多条时统一处理）。
 *
 * 非 smart 模式下任一道题对不上就返回 `null` —— 宁可让按钮禁用，也不拿
 * `options[0]` 顶替用户点击（那会把「总是允许」静默改写成「本次允许」）。
 * smart 模式保留原有兜底：退到该题的拒绝项，题干里没有拒绝项时回传 `reject`。
 */
export function buildAuthorizationAnswers(
  questions: Question[],
  picked: ResolvedAction,
  smart = false,
): UserAnswer[] | null {
  const answers: UserAnswer[] = [];
  for (const question of questions) {
    const matched = findAnswerOption(question, picked);
    const selected = matched ?? (smart ? findRejectOption(question) : undefined);
    if (!selected) {
      if (!smart) return null;
      answers.push({ question: question.question, selected_options: ['reject'] });
      continue;
    }
    answers.push({
      question: question.question,
      selected_options: [optionWireValue(selected)],
    });
  }
  return answers;
}
