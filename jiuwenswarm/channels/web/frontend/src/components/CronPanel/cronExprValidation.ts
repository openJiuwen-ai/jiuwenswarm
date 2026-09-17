// 7段式 cron 表达式校验（croniter 语法，second_at_beginning=True），原样搬自旧 CronPanel/index.tsx，
// i18n.errors.cron* key 沿用；周字段范围已按 croniter 实测结果修正（见下方说明）。

import { normalizeWeekAlphas } from './cronWeekAlpha';

// 步长（`*/N`）只要求是 ≥1 的整数，不要求能整除字段的取值范围（60/24 等）：croniter 对
// `*/9` 这类"整不除"的步长一样能正常解析、正常调度（只是在字段回绕到 0 的边界处会出现一次
// 偏短的间隔，这是 cron 语法本身的固有特性，不是不合法）。之前这里额外加了一条"stepDivisor
// 必须能被 step 整除"的自造限制，比后端（gateway/cron/cron_expr.py 用 croniter.is_valid 校验
// 语法）严格得多，导致"按间隔"选分钟填 9、选小时填 5 这类合法值被前端误判非法、"确定"按钮
// 置灰且没有对得上号的提示（见 2026-07-24 bugfix，bug001）。去掉这条限制，前端和后端的真实
// 约束就一致了。
function isValidCronField(value: string, min: number, max: number, allowQuestion: boolean = false, allowLast: boolean = false): { valid: boolean; error?: string } {
  if (value === '*') return { valid: true };
  if (allowQuestion && value === '?') return { valid: true };
  if (allowLast && value === 'L') return { valid: true };
  const parts = value.split(',');
  for (const part of parts) {
    if (part.includes('/')) {
      const [range, stepStr] = part.split('/');
      const step = parseInt(stepStr, 10);
      if (isNaN(step) || step <= 0) return { valid: false, error: getFieldError(min, max) };
      if (range === '*') continue;
      const rangeValid = isValidCronRange(range, min, max);
      if (!rangeValid) return { valid: false, error: getFieldError(min, max) };
    } else if (part.includes('-')) {
      if (!isValidCronRange(part, min, max)) return { valid: false, error: getFieldError(min, max) };
    } else {
      const num = parseInt(part, 10);
      if (isNaN(num) || num < min || num > max) return { valid: false, error: getFieldError(min, max) };
    }
  }
  return { valid: true };
}

function getFieldError(min: number, max: number): string {
  if (min === 0 && max === 59) return 'cron.errors.cronSecondOrMinute';
  if (min === 0 && max === 23) return 'cron.errors.cronHour';
  if (min === 1 && max === 31) return 'cron.errors.cronDay';
  if (min === 1 && max === 12) return 'cron.errors.cronMonth';
  if (min === 0 && max === 6) return 'cron.errors.cronWeek';
  return 'cron.errors.cronFormat';
}

function isValidCronRange(range: string, min: number, max: number): boolean {
  const [startStr, endStr] = range.split('-');
  if (!startStr || !endStr) return false;
  const start = parseInt(startStr, 10);
  const end = parseInt(endStr, 10);
  if (isNaN(start) || isNaN(end)) return false;
  if (start < min || end > max || start > end) return false;
  return true;
}

// 周字段英文缩写（SUN/MON/TUE/WED/THU/FRI/SAT，大小写不敏感）的归一化逻辑抽到了
// ./cronWeekAlpha.ts，跟 scheduleConvert.ts（cron 表达式 <-> 可视化表单双向解析）共用一套映射
// 规则，避免两处各写一份、后续又漏改其中一处。

// 周字段专用校验：在普通 isValidCronField 的基础上，额外接受"每月第几周星期几"用到的
// `{dow}#{1-5}`（第几周）和 `L{dow}`（最后一周）形状（见 scheduleConvert.ts / plan.md §2.3.8），
// 以及 croniter 原生支持的英文缩写（SUN/MON/TUE/WED/THU/FRI/SAT，大小写不敏感，见上方
// normalizeWeekAlphas；bugfix 2026072401/bug010：此前只认数字，遗漏了这条合法语法，导致对话
// 方式创建的周期任务能用缩写、手动表单却拒绝同样的表达式）。
// `{dow}#{1-5}`/`L{dow}` 这两种形状是"整段"匹配（不像普通数字/区间/步长那样可以被通用逻辑复
// 用），所以单独判断。
function isValidWeekField(value: string): { valid: boolean; error?: string } {
  if (value === '*' || value === '?') return { valid: true };
  const normalized = normalizeWeekAlphas(value);
  const parts = normalized.split(',');
  for (const part of parts) {
    // 含 # 或以 L 开头的段必须严格匹配"第几周"/"最后一周"形状，不能落到下面的通用数字解析——
    // 否则像 "1#9"（n 超出 1-5 合法范围）会被 parseInt 只认前面的 "1" 而误判成合法
    if (part.includes('#') || part.startsWith('L')) {
      const nthMatch = part.match(/^(\d)#([1-5])$/);
      if (nthMatch && Number(nthMatch[1]) <= 6) continue;
      const lastMatch = part.match(/^L(\d)$/);
      if (lastMatch && Number(lastMatch[1]) <= 6) continue;
      return { valid: false, error: 'cron.errors.cronWeek' };
    }
    const plainResult = isValidCronField(part, 0, 6);
    if (!plainResult.valid) return { valid: false, error: 'cron.errors.cronWeek' };
  }
  return { valid: true };
}

export function validateCronExpr(expr: string): { valid: boolean; error?: string } {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 7) {
    return { valid: false, error: 'cron.errors.cronFormat' };
  }
  const [second, minute, hour, day, month, week, year] = parts;
  const secondResult = isValidCronField(second, 0, 59);
  if (!secondResult.valid) return { valid: false, error: secondResult.error };
  const minuteResult = isValidCronField(minute, 0, 59);
  if (!minuteResult.valid) return { valid: false, error: minuteResult.error };
  const hourResult = isValidCronField(hour, 0, 23);
  if (!hourResult.valid) return { valid: false, error: hourResult.error };
  // day 字段允许 'L'（月末最后一天，croniter 支持，见 plan.md §2.3.1 第3点）
  const dayResult = isValidCronField(day, 1, 31, true, true);
  if (!dayResult.valid) return { valid: false, error: dayResult.error };
  const monthResult = isValidCronField(month, 1, 12);
  if (!monthResult.valid) return { valid: false, error: monthResult.error };
  // 周字段实测范围是 0-6（0=周日...6=周六），不是 Quartz 的 1-7；旧文案/校验此前写反了。
  // 用专门的 isValidWeekField（支持 ?/*、普通值、以及"每月第几周"的 #N / L 形状）
  const weekResult = isValidWeekField(week);
  if (!weekResult.valid) return { valid: false, error: weekResult.error };
  if (year !== '*') {
    const yearNum = parseInt(year, 10);
    if (isNaN(yearNum) || yearNum < 1970 || yearNum > 2099) {
      return { valid: false, error: 'cron.errors.cronYear' };
    }
  }
  return { valid: true };
}
