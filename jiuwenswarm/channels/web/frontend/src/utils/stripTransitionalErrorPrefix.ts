/**
 * 下游分类器可能按错误文案归类。过渡期内上游会在用户文案前加
 * `max_iterations_reached` 前缀；Web 展示（实时与历史恢复）需剥离该标记。
 * 待下游增加 errorCode == "max_iterations" 精确归类后，可删除本 helper 及前缀。
 */
const TRANSITIONAL_MAX_ITERATIONS_PREFIX = /^max_iterations_reached[:：]?\s*/i;

export function stripTransitionalErrorPrefix(message: string): string {
  return message.replace(TRANSITIONAL_MAX_ITERATIONS_PREFIX, '');
}
