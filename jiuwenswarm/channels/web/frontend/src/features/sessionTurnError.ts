/**
 * 会话列表失败标记（红叹号）的判定。
 *
 * 失败标记表示「这个会话最近一轮以失败收尾」。它必须有能在刷新后重建的事实来源，
 * 不能只活在 runtime.error 里——那是纯内存态，页面一刷新就没了（GitCode #6920/#6794）。
 *
 * 事实来源有两层：
 *
 * 1. 消息时间线（主）：历史恢复后 `chat.error` 落盘成带 `terminalError` 标记的 system 气泡，
 *    一轮成功收尾落 `chat.final`（assistant 气泡，恢复时打 `turnFinal` 标记），新一轮开始落
 *    user 气泡。因此「最近一轮是否失败」可以从末尾往前推导：先遇到终态错误就是失败，先遇到
 *    带 `turnFinal` 标记的完整回答就是成功。工具结果、system 通知、心跳自动轮与跨会话消息都
 *    不是人工轮次边界，跳过不看；不带 `turnFinal` 的 assistant 气泡（被中断的半截回答）也不
 *    算成功收尾，继续往前扫，避免抹掉缓存里的真实失败。
 * 2. 本地缓存（兜底）：worker 被杀（后端依据 runtime.interrupted 合成 WORKER_EXECUTION_INTERRUPTED）
 *    与 unary 拒绝（chat.send 直接 ok=false）这类失败不会落进 `chat.error` 历史记录，
 *    时间线里查不到，只能靠发送侧写进 localStorage 的那一条记录。
 *
 * 缓存的清理只认两个明确信号：用户发起新一轮（setProcessing 时清）、一轮成功收尾
 * （实时 chat.final，或历史恢复时时间线显示最近一轮正常收尾）。不做时间戳比较——
 * 历史时间来自服务端、缓存时间来自浏览器，跨机器对不齐，用时间判断容易把真实失败误判成过期。
 *
 * 见 GitCode #6920 / #6919 / #6794。
 */

import type { Message } from '../types';

const TURN_ERROR_STORAGE_KEY = 'jiuwenswarm_session_turn_error';
/** localStorage 里最多保留多少条会话记录，超出按时间丢最早的，避免无限增长。 */
const TURN_ERROR_MAX_ENTRIES = 200;

interface TurnErrorRecord {
  message: string;
  /** 失败发生时刻（epoch 毫秒），仅用于超出容量时按新旧裁剪。 */
  atMs: number;
}

/** 时间线里最近一轮的收尾方式。`unknown` 表示还没有可判定的收尾（历史为空、本轮进行中）。 */
export type LastTurnOutcome =
  | { kind: 'failed'; message: string }
  | { kind: 'succeeded' }
  | { kind: 'unknown' };

/** 不属于「当前会话当轮人工对话」的消息，不能当作轮次边界。 */
function isTransientTimelineMessage(message: Message): boolean {
  if (message.presentation === 'tool_result') return true;
  if (message.isHarnessMessage) return true;
  if (message.automation) return true;
  if (message.crossSession) return true;
  if (message.role === 'tool') return true;
  return false;
}

/**
 * 传输层/用户取消/登录预检导致的发送失败不属于「本轮以失败收尾」，不应点亮会话失败标记：
 * 这类失败可重试、也不代表 Agent 轮次真的跑过（断线、超时、用户主动取消、未登录）。
 * 其余发送失败（服务端 ok=false，例如模型调用在首帧前就抛错）就是一次真实的失败轮次。
 *
 * 见 GitCode #6919：模型调用失败走的是 unary 错误响应而不是 chat.error 事件，
 * 旧实现只在 chat.error 处理里点亮标记，于是这类失败没有红叹号。
 */
export function isTurnFailureCode(code: string | undefined | null): boolean {
  const normalized = typeof code === 'string' ? code.trim().toUpperCase() : '';
  if (!normalized) return true;
  return ![
    'REQUEST_ABORTED',
    'REQUEST_CANCELLED',
    'REQUEST_TIMEOUT',
    'AGENT_SERVER_TIMEOUT',
    'WS_DISCONNECTED',
    'WS_CLOSED',
    'WS_ERROR',
    'WS_NOT_READY',
    // 登录预检（未登录/免费模型未登录）不是本轮失败：错误气泡照常显示，
    // 但 requestLogin 已把登录框顶到用户面前，不需要点亮失败标记。
    'LOGIN_REQUIRED',
  ].includes(normalized);
}

/** 从消息时间线倒推最近一轮的收尾方式。 */
export function lastTurnOutcome(messages: readonly Message[] | undefined): LastTurnOutcome {
  if (!messages || messages.length === 0) return { kind: 'unknown' };
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (isTransientTimelineMessage(message)) continue;
    if (message.role === 'user') {
      // 回到本轮起点都没见到终态：本轮仍在进行，或历史只留下半截输入。
      return { kind: 'unknown' };
    }
    if (message.terminalError) {
      return { kind: 'failed', message: message.content || '' };
    }
    if (message.role === 'assistant') {
      // 只有带 final 语义的 assistant（chat.final 正常收尾，实时/历史恢复都打
      // turnFinal 标记）才是「本轮成功收尾」的证据。被中断的半截回答不会落盘成
      // chat.final（见 server/runtime/agent_adapter/interface.py 的「半截回答
      // 不该当 chat.final 写进历史」注释），即使历史里残留 assistant 气泡也不能
      // 据此推出 succeeded，否则会抹掉缓存里的真实失败（GitCode #6920 检视意见）。
      // 普通 assistant 不是轮次边界，继续往前找终态错误或本轮起点。
      if (message.turnFinal) {
        return { kind: 'succeeded' };
      }
      continue;
    }
    // system 通知（团队事件、目标完成卡等）不是轮次边界，继续往前找。
  }
  return { kind: 'unknown' };
}

/** 时间线里最近一轮的终态错误文案；最近一轮正常收尾（或仍在进行）时返回 null。 */
export function lastTurnErrorMessage(messages: readonly Message[] | undefined): string | null {
  const outcome = lastTurnOutcome(messages);
  return outcome.kind === 'failed' ? outcome.message : null;
}

let cache: Record<string, TurnErrorRecord> | null = null;
/** 上次解析时的原始存储内容；存储被别处改写（其它模块实例、其它标签页）时据此失效。 */
let cacheRaw: string | null = null;

function getStorage(): Storage | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return null;
    return window.localStorage;
  } catch {
    // 隐私模式/禁用存储：静默降级为纯内存态，不影响主流程。
    return null;
  }
}

function readCache(): Record<string, TurnErrorRecord> {
  let raw = '';
  try {
    raw = getStorage()?.getItem(TURN_ERROR_STORAGE_KEY) ?? '';
  } catch {
    // 存储不可读时按空缓存处理。
  }
  // 侧栏每个会话项每次渲染都会读一次，存储没变时直接复用已解析结果。
  if (cache && raw === cacheRaw) return cache;
  const next: Record<string, TurnErrorRecord> = {};
  try {
    const parsed: unknown = JSON.parse(raw || '{}');
    if (parsed && typeof parsed === 'object') {
      for (const [sessionId, value] of Object.entries(parsed as Record<string, unknown>)) {
        const record = value as Partial<TurnErrorRecord> | null;
        const message = typeof record?.message === 'string' ? record.message : '';
        if (!sessionId || !message) continue;
        const atMs = typeof record?.atMs === 'number' && Number.isFinite(record.atMs) ? record.atMs : 0;
        next[sessionId] = { message, atMs };
      }
    }
  } catch {
    // 内容损坏时当作空缓存，下一次写入会覆盖。
  }
  cache = next;
  cacheRaw = raw;
  return cache;
}

function writeCache(next: Record<string, TurnErrorRecord>): void {
  cache = next;
  try {
    const serialized = JSON.stringify(next);
    cacheRaw = serialized;
    getStorage()?.setItem(TURN_ERROR_STORAGE_KEY, serialized);
  } catch {
    // 配额满等情况静默降级：本次记录仍在内存里，刷新后会丢。
    cacheRaw = null;
  }
}

/** 记下某个会话「最近一轮以失败收尾」；atMs 缺省用当前时刻。 */
export function rememberSessionTurnError(sessionId: string, message: string, atMs?: number): void {
  if (!sessionId || !message) return;
  const current = readCache();
  const recordedAt =
    typeof atMs === 'number' && Number.isFinite(atMs) && atMs > 0 ? atMs : Date.now();
  const previous = current[sessionId];
  if (previous && previous.message === message && previous.atMs === recordedAt) return;
  const next: Record<string, TurnErrorRecord> = {
    ...current,
    [sessionId]: { message, atMs: recordedAt },
  };
  const entries = Object.entries(next);
  if (entries.length > TURN_ERROR_MAX_ENTRIES) {
    entries.sort((left, right) => right[1].atMs - left[1].atMs);
    writeCache(Object.fromEntries(entries.slice(0, TURN_ERROR_MAX_ENTRIES)));
    return;
  }
  writeCache(next);
}

/** 会话的失败已被新一轮覆盖（用户重新发送、或成功收尾）时清掉缓存记录。 */
export function forgetSessionTurnError(sessionId: string): void {
  if (!sessionId) return;
  const current = readCache();
  if (!(sessionId in current)) return;
  const next = { ...current };
  delete next[sessionId];
  writeCache(next);
}

/**
 * 该会话当前的失败文案：优先看消息时间线（刷新后仍然存在的事实），
 * 时间线没有可判定的收尾时才回落到本地缓存（unary 拒绝、worker 被杀这类
 * 只活在发送侧的失败）。时间线明确显示「最近一轮成功」时不看缓存。
 *
 * 纯读，可以在渲染里调用。
 */
export function currentSessionTurnError(
  sessionId: string,
  messages?: readonly Message[],
): string | null {
  const outcome = lastTurnOutcome(messages);
  if (outcome.kind === 'failed') return outcome.message;
  if (outcome.kind === 'succeeded') return null;
  return readCache()[sessionId]?.message ?? null;
}

/**
 * 历史恢复时用时间线校准缓存，返回该会话当前的失败文案。
 *
 * - 时间线显示最近一轮失败 → 以历史为准写回缓存（历史是权威事实来源）；
 * - 时间线显示最近一轮正常收尾 → 缓存里的旧失败作废（重试成功后刷新不该再亮红叹号）；
 * - 时间线没有可判定的收尾（空历史 / 本轮还在跑）→ 保留缓存，让后端没落历史的失败仍能显示。
 */
export function reconcileSessionTurnError(
  sessionId: string,
  messages: readonly Message[] | undefined,
): string | null {
  const outcome = lastTurnOutcome(messages);
  if (outcome.kind === 'failed') {
    rememberSessionTurnError(sessionId, outcome.message);
    return outcome.message;
  }
  if (outcome.kind === 'succeeded') {
    forgetSessionTurnError(sessionId);
    return null;
  }
  return readCache()[sessionId]?.message ?? null;
}

/**
 * 侧栏会话项要展示的失败文案（红叹号的 tooltip）。
 *
 * 取值顺序：
 * 1. runtime.error / runtime.executionError —— 本次页面存活期间的实时失败，文案最贴近当次错误；
 * 2. 消息时间线推导 —— 刷新/切回会话后重建出来的失败（#6920/#6794 的正题）；
 * 3. 本地缓存 —— 后端没落历史的失败（unary 拒绝、worker 被杀，见 #6919/#6794）。
 *
 * 正在进行的一轮不推导：新一轮开始时 setProcessing(true) 已清掉 runtime.error，
 * 此时旧失败不该继续挂在重试中的会话上。
 */
export function sessionFailureMessage(
  sessionId: string,
  runtime?: {
    error?: string | null;
    executionError?: string | null;
    isProcessing?: boolean;
    messages?: readonly Message[];
  },
): string | null {
  const live = runtime?.error || runtime?.executionError;
  if (live) return live;
  if (runtime?.isProcessing) return null;
  return currentSessionTurnError(sessionId, runtime?.messages);
}
