/**
 * 技能总谱面板的后端轮询判定。
 *
 * 面板在左边栏切走后仍保持挂载（`App.tsx` 只是加 `is-hidden` 类），因此轮询
 * 必须显式依赖页面激活状态。否则用户离开技能页后，`skills.graph.status`
 * 仍会持续请求。
 */

export type SkillGraphPollInput = {
  /** 当前技能页面是否激活（左边栏选中技能）。 */
  isActive: boolean;
  /** 总谱是否正在更新。 */
  updating: boolean;
  /** 该轮询只在 `updating` 等于此值时运行。 */
  whenUpdating: boolean;
};

/** 返回该轮询在当前激活/更新状态下是否应运行。 */
export function shouldPollSkillGraph({ isActive, updating, whenUpdating }: SkillGraphPollInput): boolean {
  return isActive && updating === whenUpdating;
}
