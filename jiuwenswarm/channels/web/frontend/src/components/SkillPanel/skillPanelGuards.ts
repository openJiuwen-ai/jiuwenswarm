/**
 * SkillPanel 请求过期与选中回写的守卫逻辑（纯函数，供组件与测试共用）。
 *
 * 背景：技能详情请求（skills.get / skills.swarmskillshub.detail）走 WS 往返，
 * 慢响应晚到时会把已切换到别的技能的详情页覆盖回旧技能；启停开关的回写
 * 则闭包了点击时的旧 selectedSkill。这里的工具把两类判断抽成可测的纯逻辑：
 * - `SequenceGuard`：序号守卫，晚到的响应不再写状态；
 * - `applyToggleToSelection`：按“当前选中”而非点击时闭包决定是否回写。
 */

/** 单调序号守卫：begin 开启新一轮请求并作废之前所有轮次。 */
export class SequenceGuard {
  private current = 0;

  /** 开始新请求，返回本轮序号；此前发出的序号全部过期。 */
  begin(): number {
    this.current += 1;
    return this.current;
  }

  /** 无新请求地作废在途轮次（关闭详情页等场景），此后所有 isCurrent 为 false。 */
  invalidate(): void {
    this.current += 1;
  }

  /** seq 是否仍是最新一轮。 */
  isCurrent(seq: number): boolean {
    return seq === this.current;
  }
}

/**
 * 启停回写：仅当“当前选中”仍是被切换的技能本身时翻转 enabled，否则原样返回。
 * 用当前选中而非点击时的闭包判断，避免 await 之后把已经切走的详情页拉回旧技能。
 */
export function applyToggleToSelection<S extends { name: string; enabled?: boolean }>(
  current: S | null,
  skillName: string,
  newEnabled: boolean,
): S | null {
  if (!current || current.name !== skillName) return current;
  return { ...current, enabled: newEnabled };
}
