/**
 * 技能重建等待循环（从 SkillPanel/index.tsx 抽出，纯逻辑可测）。
 *
 * 背景：重建是后台静默执行，前端靠轮询 skills.rebuild_status 等待完成，
 * 最长 30 分钟。原实现是不可取消的 while 循环：面板卸载后仍继续后台
 * RPC，完成后的回写会把用户正在看的其他技能详情覆盖回旧技能。
 * 这里注入 sleep / 状态请求 / 取消谓词，使循环可在卸载或被新一轮
 * 重建取代时退出，行为语义与原实现保持一致（done / failed / timeout）。
 */
import type { SkillRebuildResponse } from './types';

export type RebuildWaitOutcome = 'done' | 'timeout' | 'cancelled';

export interface RebuildWaitOptions {
  /** 轮询间隔（毫秒）。 */
  pollMs: number;
  /** 总等待上限（毫秒）。 */
  waitMs: number;
  /** 轮询睡眠，测试注入假时钟。 */
  sleep: (ms: number) => Promise<void>;
  /** 状态查询，测试注入假响应。 */
  requestStatus: (name: string, rebuildId: string) => Promise<SkillRebuildResponse>;
  /** 返回 true 时终止等待（组件卸载 / 被新一轮重建取代）。 */
  isCancelled?: () => boolean;
  /** 当前时间（毫秒），测试注入假时钟。 */
  now?: () => number;
}

/**
 * 等待重建完成。返回 'done'（成功完成）、'timeout'（超时仍在跑）或
 * 'cancelled'（等待期间被取消，调用方不应再做任何回写或提示）。
 * 重建失败时与原实现一致地抛出错误。
 */
export async function waitForSkillRebuild(
  name: string,
  rebuildId: string,
  options: RebuildWaitOptions,
): Promise<RebuildWaitOutcome> {
  const now = options.now ?? Date.now;
  const deadline = now() + options.waitMs;
  while (now() < deadline) {
    await options.sleep(options.pollMs);
    if (options.isCancelled?.()) return 'cancelled';
    const status = await options.requestStatus(name, rebuildId);
    if (options.isCancelled?.()) return 'cancelled';
    if (status.status === 'done' && status.success) return 'done';
    if (status.status === 'failed' || status.success === false) {
      throw new Error(status.detail || '技能重建失败。');
    }
  }
  return 'timeout';
}
