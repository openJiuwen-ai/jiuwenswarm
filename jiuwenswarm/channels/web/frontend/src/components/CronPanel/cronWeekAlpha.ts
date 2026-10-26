// 周字段（DOW）英文缩写 <-> 数字 归一化，供 cronExprValidation.ts（合法性校验）和
// scheduleConvert.ts（cron 表达式 <-> "周期/按间隔/单次"可视化表单 双向解析）共用，避免两处各写
// 一份、后续又漏改其中一处（见 bugfix 2026072401/bug010：第 3 轮修复暴露的正是"cronExprValidation.ts
// 已经支持缩写，但 scheduleConvert.ts 没跟着改，导致表达式能建、可视化 Tab 却认不出"这个不一致）。
//
// 映射取自 croniter 的 DOW_ALPHAS（sun=0...sat=6），跟本项目周字段"0=周日...6=周六"的语义一致，
// 大小写不敏感。用词边界（\b...\b）匹配，刻意不识别两种 croniter 本身也不支持的写法：
// - `L` + 缩写（如 `LMON`）：`L` 和 `mon` 之间没有词边界（都是 \w），不会被替换；
// - 英文全称（如 `MONDAY`）：`mon` 后面紧跟 `day`（都是 \w），词边界不满足，不会被替换。
// 这两种保持原样后交给各自的数字校验/解析逻辑处理，自然会被判定为不合法/无法识别，和后端行为一致。
const WEEK_DOW_ALPHA_TO_NUM: Record<string, string> = {
  sun: '0',
  mon: '1',
  tue: '2',
  wed: '3',
  thu: '4',
  fri: '5',
  sat: '6',
};
const WEEK_DOW_ALPHA_PATTERN = /\b(sun|mon|tue|wed|thu|fri|sat)\b/gi;

export function normalizeWeekAlphas(value: string): string {
  return value.replace(WEEK_DOW_ALPHA_PATTERN, (match) => WEEK_DOW_ALPHA_TO_NUM[match.toLowerCase()]);
}
