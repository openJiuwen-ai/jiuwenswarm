import type { CronSchedule } from '../../types/cron';
import { normalizeWeekAlphas } from './cronWeekAlpha';

// schedule ⇄ cron_expr（7段式：秒 分 时 日 月 周 年，croniter 语法）双向转换。
// 星期字段用 croniter 实测出的真实编号：0=周日...6=周六（不是 Quartz 的 1=SUN），见 plan.md §2.3.1。
// 6 种模式各自占据互不冲突的字段形状（是否有步长/日或月是否具体/年是否固定），可以无损双向转换；
// 匹配不到任何一种形状的表达式（比如 Agent 工具/TUI 建的、或日周字段同时给了具体值的）返回 null，
// 调用方应当回退展示"Cron表达式"原文，不强行拆解。

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

function parseTime(time: string | undefined): { h: number; m: number } | null {
  if (!time) return null;
  const match = time.match(/^(\d{1,2}):(\d{1,2})$/);
  if (!match) return null;
  const h = Number(match[1]);
  const m = Number(match[2]);
  if (h < 0 || h > 23 || m < 0 || m > 59) return null;
  return { h, m };
}

function isWildcard(field: string): boolean {
  return field === '*' || field === '?';
}

/** field 是单个具体整数（不含逗号/区间/步长），返回该数字；否则 null */
function parseSingleInt(field: string): number | null {
  if (!/^\d+$/.test(field)) return null;
  return Number(field);
}

/** field 是逗号分隔的整数列表（不含区间/步长），返回排序去重后的数组；否则 null */
function parseIntList(field: string): number[] | null {
  const parts = field.split(',');
  const nums: number[] = [];
  for (const p of parts) {
    if (!/^\d+$/.test(p)) return null;
    nums.push(Number(p));
  }
  if (nums.length === 0) return null;
  return Array.from(new Set(nums)).sort((a, b) => a - b);
}

/**
 * field 是"每月第几周的星期几"形状（`{dow}#{1-5}` 或 `L{dow}`，逗号分隔多个星期几），
 * 且所有段用的是同一个"第几周"（不支持混用，见 plan.md §2.3.8 第3点）；否则 null。
 */
function parseMonthlyWeekdayField(field: string): { weekdays: number[]; weekOfMonth: number | 'L' } | null {
  const parts = field.split(',');
  const weekdays: number[] = [];
  let weekOfMonth: number | 'L' | null = null;
  for (const p of parts) {
    const hashMatch = p.match(/^(\d)#([1-5])$/);
    const lastMatch = p.match(/^L(\d)$/);
    if (hashMatch) {
      const dow = Number(hashMatch[1]);
      const n = Number(hashMatch[2]);
      if (dow > 6) return null;
      if (weekOfMonth === null) weekOfMonth = n;
      else if (weekOfMonth !== n) return null;
      weekdays.push(dow);
    } else if (lastMatch) {
      const dow = Number(lastMatch[1]);
      if (dow > 6) return null;
      if (weekOfMonth === null) weekOfMonth = 'L';
      else if (weekOfMonth !== 'L') return null;
      weekdays.push(dow);
    } else {
      return null;
    }
  }
  if (weekOfMonth === null || weekdays.length === 0) return null;
  return { weekdays: Array.from(new Set(weekdays)).sort((a, b) => a - b), weekOfMonth };
}

export function scheduleToCronExpr(schedule: CronSchedule): string {
  switch (schedule.kind) {
    case 'daily': {
      const t = parseTime(schedule.time);
      if (!t) return '';
      return `0 ${t.m} ${t.h} * * ? *`;
    }
    case 'weekly': {
      const t = parseTime(schedule.time);
      if (!t || !schedule.weekdays || schedule.weekdays.length === 0) return '';
      const days = Array.from(new Set(schedule.weekdays)).sort((a, b) => a - b).join(',');
      return `0 ${t.m} ${t.h} ? * ${days} *`;
    }
    case 'monthly': {
      const t = parseTime(schedule.time);
      if (!t || schedule.day === undefined) return '';
      return `0 ${t.m} ${t.h} ${schedule.day} * ? *`;
    }
    case 'monthlyWeekday': {
      const t = parseTime(schedule.time);
      if (!t || !schedule.weekdays || schedule.weekdays.length === 0 || schedule.weekOfMonth === undefined) return '';
      const days = Array.from(new Set(schedule.weekdays)).sort((a, b) => a - b)
        .map((d) => (schedule.weekOfMonth === 'L' ? `L${d}` : `${d}#${schedule.weekOfMonth}`))
        .join(',');
      return `0 ${t.m} ${t.h} ? * ${days} *`;
    }
    case 'yearly': {
      const t = parseTime(schedule.time);
      if (!t || !schedule.month || !schedule.day) return '';
      return `0 ${t.m} ${t.h} ${schedule.day} ${schedule.month} ? *`;
    }
    case 'interval': {
      const days = schedule.weekdays && schedule.weekdays.length > 0
        ? Array.from(new Set(schedule.weekdays)).sort((a, b) => a - b).join(',')
        : '*';
      // 显式判断而不是 `!schedule.everyMinutes`/`!schedule.everyHours`：0 和 undefined 在语义上
      // 都是"无效间隔"，但用显式的 `< 1` 表达出来，不依赖假值隐式转换，逻辑更清楚，也方便以后
      // 需要更精确的报错文案时复用同一个判断（见 2026-07-23 bugfix，bug002）。
      if (schedule.intervalUnit === 'minutes') {
        if (schedule.everyMinutes === undefined || schedule.everyMinutes < 1) return '';
        return `0 */${schedule.everyMinutes} * * * ${days} *`;
      }
      if (schedule.everyHours === undefined || schedule.everyHours < 1) return '';
      return `0 0 */${schedule.everyHours} * * ${days} *`;
    }
    case 'once': {
      const t = parseTime(schedule.time);
      if (!t || !schedule.date) return '';
      const m = schedule.date.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
      if (!m) return '';
      const [, y, mo, d] = m;
      return `0 ${t.m} ${t.h} ${Number(d)} ${Number(mo)} ? ${y}`;
    }
    default:
      return '';
  }
}

export function cronExprToSchedule(expr: string): CronSchedule | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 7) return null;
  const [second, minute, hour, day, month, weekRaw, year] = parts;
  // 周字段可能带英文缩写（SUN/MON/.../SAT，大小写不敏感，见 cronExprValidation.ts 里
  // isValidWeekField 的同款归一化——bugfix 2026072401/bug010 第 4 轮：cron 表达式校验那边已经
  // 支持缩写、能创建成功，但这里解析成"周期/按间隔/单次"可视化表单时如果不归一化，
  // parseIntList/parseMonthlyWeekdayField 只认纯数字，会把 MON 当成无法识别，导致可视化 Tab
  // 显示不出对应的星期选项）。先归一化成数字，下面的解析逻辑不用再改。
  const week = normalizeWeekAlphas(weekRaw);
  // 秒字段只要求"是单个具体数字"（不含 */,- 等形状），具体数值不参与结构化模型——
  // 结构化 tab 本身不提供秒级精度，生成时永远写 0；但识别时不应该因为秒不是 0
  // 就整体拒绝，否则像 Agent 工具/OpenClaw "at" 调度产生的、秒字段带着原始时间戳
  // 秒数的单次 cron（如 `2 4 12 16 7 ? 2026`）会被误判成"识别不了"而只能看 Cron
  // 表达式原文，实际上它的日/月/年形状清楚地是"单次"（见 2026-07-16 bugfix）。
  if (parseSingleInt(second) === null) return null;

  const min = parseSingleInt(minute);
  const hr = parseSingleInt(hour);

  // once：年份是固定值
  if (!isWildcard(year)) {
    const y = parseSingleInt(year);
    const d = parseSingleInt(day);
    const mo = parseSingleInt(month);
    if (y === null || d === null || mo === null || hr === null || min === null) return null;
    if (!isWildcard(week)) return null;
    return { kind: 'once', time: `${pad2(hr)}:${pad2(min)}`, date: `${y}-${pad2(mo)}-${pad2(d)}` };
  }

  // interval：小时字段带步长（每N小时）
  if (hour.startsWith('*/')) {
    if (minute !== '0') return null;
    const n = parseSingleInt(hour.slice(2));
    if (n === null || !isWildcard(day) || month !== '*') return null;
    if (isWildcard(week)) {
      return { kind: 'interval', intervalUnit: 'hours', everyHours: n };
    }
    const days = parseIntList(week);
    if (!days) return null;
    return { kind: 'interval', intervalUnit: 'hours', everyHours: n, weekdays: days };
  }

  // interval：分钟字段带步长、小时字段通配（每N分钟）
  if (minute.startsWith('*/') && hour === '*') {
    const n = parseSingleInt(minute.slice(2));
    if (n === null || !isWildcard(day) || month !== '*') return null;
    if (isWildcard(week)) {
      return { kind: 'interval', intervalUnit: 'minutes', everyMinutes: n };
    }
    const days = parseIntList(week);
    if (!days) return null;
    return { kind: 'interval', intervalUnit: 'minutes', everyMinutes: n, weekdays: days };
  }

  if (hr === null || min === null) return null;
  const time = `${pad2(hr)}:${pad2(min)}`;

  // 日字段通配、周字段非通配：先试"每月第几周星期几"形状，再试普通"每周"形状
  if (isWildcard(day) && month === '*' && !isWildcard(week)) {
    const monthlyWeekday = parseMonthlyWeekdayField(week);
    if (monthlyWeekday) {
      return { kind: 'monthlyWeekday', time, weekdays: monthlyWeekday.weekdays, weekOfMonth: monthlyWeekday.weekOfMonth };
    }
    const days = parseIntList(week);
    if (!days) return null;
    return { kind: 'weekly', time, weekdays: days };
  }

  // daily：日、月、周都通配
  if (isWildcard(day) && month === '*' && isWildcard(week)) {
    return { kind: 'daily', time };
  }

  // monthly：日字段是具体数字或 'L'，月字段通配
  if ((day === 'L' || parseSingleInt(day) !== null) && month === '*' && isWildcard(week)) {
    return { kind: 'monthly', time, day: day === 'L' ? 'L' : Number(day) };
  }

  // yearly：日、月字段都是具体数字
  const d = parseSingleInt(day);
  const mo = parseSingleInt(month);
  if (d !== null && mo !== null && isWildcard(week)) {
    return { kind: 'yearly', time, day: d, month: mo };
  }

  return null;
}

/**
 * 给定时区当前的墙钟时间，格式为 "YYYY-MM-DDTHH:mm"（零填充）。
 * 用来跟"单次"排班的 date+time 做字符串比较——两者都是零填充的 ISO 前缀，
 * 按字典序比较等价于按时间先后比较，不需要引入日期库做时区换算。
 * 用 hourCycle: 'h23' 而不是 hour12: false，避免个别引擎在午夜把小时格式化成 "24" 而不是 "00"。
 */
export function nowWallClock(timezone: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`;
}

/** "单次"排班的日期+时间是否已经过去（按 schedule 所在时区的当前墙钟时间比较）。 */
export function isOnceScheduleExpired(schedule: CronSchedule, timezone: string): boolean {
  if (schedule.kind !== 'once' || !schedule.date || !schedule.time) return false;
  return `${schedule.date}T${schedule.time}` <= nowWallClock(timezone);
}

type TFn = (key: string, options?: Record<string, unknown>) => string;

// 星期真实编号（0=周日...6=周六）→ i18n key 后缀，供拼接"每周一、三、五"这样的摘要文案用
const WEEKDAY_KEY_BY_VALUE: Record<number, string> = {
  0: 'sun', 1: 'mon', 2: 'tue', 3: 'wed', 4: 'thu', 5: 'fri', 6: 'sat',
};

function joinWeekdays(weekdays: number[], t: TFn): string {
  return weekdays
    .map((d) => t(`cron.schedule.weekday.${WEEKDAY_KEY_BY_VALUE[d]}`))
    .join(t('cron.schedule.listSeparator'));
}

/**
 * 把结构化 schedule 转成人类可读的摘要文案（"每天 09:30"这种），用于任务列表"计划于"列展示，
 * 替代直接显示 7 段式 cron_expr 原文——原文对普通用户来说太不直观了。
 * 只有 index.tsx 的 cronExprToSchedule(job.cronExpr) 解析成功时才调用这个函数；
 * 解析不出来的（非 6 种模式生成的表达式）由调用方自行回退展示 cron_expr 原文。
 */
export function summarizeSchedule(schedule: CronSchedule, t: TFn): string {
  switch (schedule.kind) {
    case 'daily':
      return t('cron.schedule.summary.daily', { time: schedule.time ?? '' });
    case 'weekly':
      return t('cron.schedule.summary.weekly', {
        days: joinWeekdays(schedule.weekdays ?? [], t),
        time: schedule.time ?? '',
      });
    case 'monthly':
      return t('cron.schedule.summary.monthly', {
        // "每月" + "月末最后一天" 会重复"月"字，摘要场景下用更短的"月末"
        day: schedule.day === 'L' ? t('cron.schedule.summary.lastDay') : t('cron.schedule.dayOption', { day: schedule.day }),
        time: schedule.time ?? '',
      });
    case 'monthlyWeekday':
      return t('cron.schedule.summary.monthlyWeekday', {
        weekOfMonth: t(`cron.schedule.weekOfMonth.${schedule.weekOfMonth === 'L' ? 'last' : schedule.weekOfMonth}`),
        days: joinWeekdays(schedule.weekdays ?? [], t),
        time: schedule.time ?? '',
      });
    case 'yearly':
      return t('cron.schedule.summary.yearly', {
        month: schedule.month ?? '',
        day: schedule.day ?? '',
        time: schedule.time ?? '',
      });
    case 'interval': {
      const isMinutes = schedule.intervalUnit === 'minutes';
      const n = isMinutes ? schedule.everyMinutes ?? 1 : schedule.everyHours ?? 1;
      const withWeekdaysKey = isMinutes ? 'cron.schedule.summary.intervalMinutesWithWeekdays' : 'cron.schedule.summary.intervalWithWeekdays';
      const plainKey = isMinutes ? 'cron.schedule.summary.intervalMinutes' : 'cron.schedule.summary.interval';
      return schedule.weekdays && schedule.weekdays.length > 0
        ? t(withWeekdaysKey, { n, days: joinWeekdays(schedule.weekdays, t) })
        : t(plainKey, { n });
    }
    case 'once':
      return t('cron.schedule.summary.once', { date: schedule.date ?? '', time: schedule.time ?? '' });
    default:
      return '';
  }
}
