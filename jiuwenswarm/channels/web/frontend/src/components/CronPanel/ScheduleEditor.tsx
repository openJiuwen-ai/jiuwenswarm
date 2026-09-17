import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import SimpleSelect from './SimpleSelect';
import TimePicker from './TimePicker';
import DatePicker from './DatePicker';
import { validateCronExpr } from './cronExprValidation';
import {
  normalizeWakeOffsetMinutesInput,
  wakeOffsetMinutesToSeconds,
  wakeOffsetSecondsToMinutes,
} from './cronWakeOffset';
import { scheduleToCronExpr, cronExprToSchedule, nowWallClock } from './scheduleConvert';
import type { CronSchedule, CronScheduleKind } from '../../types/cron';

interface ScheduleEditorProps {
  value: string; // cron_expr 原文，唯一提交给后端的数据
  onChange: (v: string) => void;
  timezone: string; // "单次"tab 用来算"今天/现在"，禁掉已经过去的日期和时间点（见 2026-07-16 bugfix）
  /** 提前唤醒秒数（后端 wake_offset_seconds）；UI 以分钟展示 */
  wakeOffsetSeconds?: number;
  onWakeOffsetSecondsChange?: (seconds: number) => void;
  /** 仅锁定提前唤醒（proactive.tick 等不允许改 wake_offset） */
  wakeOffsetDisabled?: boolean;
}

type TopMode = 'period' | 'interval' | 'once' | 'cronExpr';

const PERIOD_KINDS: Extract<CronScheduleKind, 'daily' | 'weekly' | 'monthly' | 'yearly'>[] = [
  'daily', 'weekly', 'monthly', 'yearly',
];

// croniter 实测的真实星期编号：0=周日...6=周六（见 plan.md §2.3.1），按钮显示顺序用"一二三四五六日"
const WEEKDAY_ITEMS: { value: number; key: string }[] = [
  { value: 1, key: 'mon' }, { value: 2, key: 'tue' }, { value: 3, key: 'wed' },
  { value: 4, key: 'thu' }, { value: 5, key: 'fri' }, { value: 6, key: 'sat' },
  { value: 0, key: 'sun' },
];

// "每月第几周"选项：不提供"第五周"（croniter 的 #5 在没有第5次出现的月份会整月跳过，不可靠，
// 见 plan.md §2.3.8 第1点），只给第一~四周 + 最后一周（后者是独立的 L{dow} 语法，行为可靠）
const WEEK_OF_MONTH_OPTIONS: { value: string; key: string }[] = [
  { value: '1', key: '1' }, { value: '2', key: '2' }, { value: '3', key: '3' }, { value: '4', key: '4' },
  { value: 'L', key: 'last' },
];

function topModeOf(kind: CronScheduleKind): TopMode {
  if (kind === 'interval') return 'interval';
  if (kind === 'once') return 'once';
  return 'period';
}

function defaultForTopMode(mode: 'period' | 'interval' | 'once'): CronSchedule {
  if (mode === 'interval') return { kind: 'interval', intervalUnit: 'hours', everyHours: 1 };
  if (mode === 'once') return { kind: 'once', time: '', date: '' };
  return { kind: 'daily', time: '' };
}

// "按间隔"的数字输入：按当前单位（小时/分钟）取对应字段的文本表示，两者二选一
function intervalNumberTextOf(schedule: CronSchedule): string {
  if (schedule.kind !== 'interval') return '';
  const n = schedule.intervalUnit === 'minutes' ? schedule.everyMinutes : schedule.everyHours;
  return n !== undefined ? String(n) : '';
}

// 去掉纯数字字符串的前导零，只保留数值本身的字符串形式：""→""，"0"→"0"，"00"→"0"，
// "01"→"1"，"010"→"10"。用 Number(...) 往返一次即可，不用手写正则去处理"全 0"这种边界
// （见 2026-07-23 bugfix 追加需求，bug002：用户先敲 0 再敲其它数字时，输入框不应该停留在
// "01"这种带前导零的形式上）。
function normalizeIntegerDigits(digitsOnly: string): string {
  return digitsOnly === '' ? '' : String(Number(digitsOnly));
}

function WeekdayPicker({ selected, onToggle }: { selected: number[]; onToggle: (day: number) => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex min-w-0 flex-1 gap-1.5">
      {WEEKDAY_ITEMS.map(({ value, key }) => {
        const active = selected.includes(value);
        return (
          <button
            key={value}
            type="button"
            onClick={() => onToggle(value)}
            data-testid="cron-schedule-weekday-btn"
            data-variant={key}
            className={`h-9 min-w-0 flex-1 rounded-md border text-sm transition-colors ${
              active
                ? 'border-accent bg-accent-subtle text-accent'
                : 'border-border bg-card text-text hover:border-border-strong'
            }`}
          >
            {t(`cron.schedule.weekday.${key}`)}
          </button>
        );
      })}
    </div>
  );
}

// 高保真设计的执行计划编辑器有 4 个 tab：周期/按间隔/单次/Cron表达式。前 3 个是结构化编辑，
// 最后一个是直接编辑 cron_expr 原文的兜底/高级模式（编辑任务时若原表达式无法结构化识别，
// 或用户手动切到这个 tab，都以它为准，见 scheduleConvert.ts 的反向解析策略）。
export default function ScheduleEditor({
  value,
  onChange,
  timezone,
  wakeOffsetSeconds = 0,
  onWakeOffsetSecondsChange,
  wakeOffsetDisabled = false,
}: ScheduleEditorProps) {
  const { t } = useTranslation();
  const initialParsed = cronExprToSchedule(value);
  const initialSchedule = initialParsed ?? { kind: 'daily', time: '' };
  const [schedule, setSchedule] = useState<CronSchedule>(initialSchedule);
  // 分钟输入单独用文本态，便于清空重输；能解析成非负整数才回写秒数
  const wakeMinutesFromProps = wakeOffsetSecondsToMinutes(wakeOffsetSeconds);
  const [wakeOffsetMinutesText, setWakeOffsetMinutesText] = useState(() => String(wakeMinutesFromProps));
  // 父表单换任务 / 外部改秒数时，把展示文本同步回来（本组件自身 onChange 写回的同值不会抖动）
  useEffect(() => {
    setWakeOffsetMinutesText(String(wakeMinutesFromProps));
  }, [wakeMinutesFromProps]);
  // 默认 tab：能解析出结构化 schedule 就跟它走；解析不出来时，创建任务（value 为空）默认落在
  // "周期"而不是"Cron表达式"（更符合大多数人的心智，表达式 tab 留给"手写/编辑一条解析不了的旧
  // 表达式"这种进阶场景）；编辑一条解析不出来的已有表达式（value 非空但 parse 失败）则仍然落在
  // "Cron表达式"tab，让用户能看到并编辑原文，不能默默换成一个和原表达式无关的默认周期排班。
  const [topMode, setTopMode] = useState<TopMode>(
    initialParsed ? topModeOf(initialParsed.kind) : value.trim() ? 'cronExpr' : 'period',
  );
  // "按间隔"数字输入框单独存一份文本状态，只做"显示"，输入时先过滤掉非数字字符（小时步长/分钟步长
  // 在 croniter 里都只支持整数，见 cronExprValidation.ts），能解析成数字才同步进 schedule。
  const [intervalNumberText, setIntervalNumberText] = useState(() => intervalNumberTextOf(initialSchedule));

  // 切 tab 时的"上一次编辑内容"缓存：按 topMode 分桶各存一份 schedule。原实现切走一个 tab 时
  // 直接用 defaultForTopMode 把 value（唯一数据源）整个覆盖成空白默认值，原 tab 的数据没有任何
  // 备份，切回来自然拿不到（见 2026-07-16 bugfix）。这里给"周期/按间隔/单次"三个 tab 各留一份
  // 记忆，切换时优先用缓存恢复，缓存没有（该 tab 还没被访问过）才用默认值。
  const savedByModeRef = useRef<Partial<Record<'period' | 'interval' | 'once', CronSchedule>>>(
    initialParsed ? { [topModeOf(initialParsed.kind)]: initialParsed } : {},
  );

  const validation = value.trim() ? validateCronExpr(value) : { valid: true };

  // "单次"tab 用："今天"之前的日期整体不可选；选中日期正好是今天时，"现在"之前的时间点也不可选
  // （见 2026-07-16 bugfix：与其等提交时才提示已过期，不如直接不让选上）
  const nowStr = nowWallClock(timezone);
  const todayStr = nowStr.slice(0, 10);
  const nowTimeStr = nowStr.slice(11, 16);

  // cacheMode 默认取当前 topMode——这对绝大多数调用（在同一个 tab 内编辑字段）是对的。
  // 但 switchTopMode 里 setTopMode(mode) 是异步的，这个函数体内闭包捕获的 topMode 在同一次
  // 事件处理里仍是切换前的旧值；如果这里继续按闭包里的旧 topMode 去写缓存，会把"即将切入的新
  // schedule"错误地存进"切走的旧 tab"的缓存桶，污染旧桶，且新 tab 自己的桶始终没被正确写入——
  // 多切几次 tab 后 topMode 和 schedule.kind 就会彻底对不上，导致 tab 内容整个不渲染
  // （见 2026-07-16 bugfix）。所以 switchTopMode 必须显式把"即将切入的 mode"传进来。
  function updateSchedule(next: CronSchedule, cacheMode: TopMode = topMode) {
    setSchedule(next);
    if (cacheMode !== 'cronExpr') savedByModeRef.current[cacheMode] = next;
    onChange(scheduleToCronExpr(next));
  }

  function switchTopMode(mode: 'period' | 'interval' | 'once') {
    if (mode === topMode) return; // 已经在这个 tab 上，不要用当前（可能不完整）的 value 重新解析覆盖
    // 优先级：当前 value 能直接解析成目标 tab 的形状（比如用户在"Cron表达式"tab 手写后切回结构化
    // tab）> 该 tab 之前编辑过的缓存 > 默认值
    const parsed = cronExprToSchedule(value);
    const next = parsed && topModeOf(parsed.kind) === mode
      ? parsed
      : savedByModeRef.current[mode] ?? defaultForTopMode(mode);
    setTopMode(mode);
    if (mode === 'interval') setIntervalNumberText(intervalNumberTextOf(next));
    updateSchedule(next, mode);
  }

  // "按间隔"的小时/分钟单位切换：数字重置为空，避免"同一串数字换个单位该怎么解读"的歧义
  function setIntervalUnit(unit: 'hours' | 'minutes') {
    if (schedule.kind !== 'interval' || (schedule.intervalUnit ?? 'hours') === unit) return;
    setIntervalNumberText('');
    updateSchedule(
      unit === 'minutes'
        ? { kind: 'interval', intervalUnit: 'minutes', everyMinutes: undefined, weekdays: schedule.weekdays }
        : { kind: 'interval', intervalUnit: 'hours', everyHours: undefined, weekdays: schedule.weekdays },
    );
  }

  function setPeriodKind(kind: Extract<CronScheduleKind, 'daily' | 'weekly' | 'monthly' | 'yearly'>) {
    const time = 'time' in schedule ? schedule.time ?? '' : '';
    if (kind === 'daily') updateSchedule({ kind: 'daily', time });
    else if (kind === 'weekly') updateSchedule({ kind: 'weekly', time, weekdays: [] });
    // 从其它类型切到"每月"，默认停在"按日期"子模式（不管切之前是不是"按星期"）
    else if (kind === 'monthly') updateSchedule({ kind: 'monthly', time, day: 1 });
    else updateSchedule({ kind: 'yearly', time, month: 1, day: 1 });
  }

  // "每月"内部的"按日期/按星期"二级切换；只在 schedule.kind 是 monthly/monthlyWeekday 时会被调用
  function setMonthlySubMode(mode: 'date' | 'week') {
    const time = schedule.time ?? '';
    if (mode === 'date') updateSchedule({ kind: 'monthly', time, day: 1 });
    else updateSchedule({ kind: 'monthlyWeekday', time, weekOfMonth: 1, weekdays: [] });
  }

  function toggleWeekday(day: number) {
    if (schedule.kind !== 'weekly' && schedule.kind !== 'interval' && schedule.kind !== 'monthlyWeekday') return;
    const current = schedule.weekdays ?? [];
    const next = current.includes(day) ? current.filter((d) => d !== day) : [...current, day].sort((a, b) => a - b);
    updateSchedule({ ...schedule, weekdays: next });
  }

  const dayOptions = [
    ...Array.from({ length: 31 }, (_, i) => ({ value: String(i + 1), label: t('cron.schedule.dayOption', { day: i + 1 }) })),
    { value: 'L', label: t('cron.schedule.lastDayOfMonth') },
  ];
  const monthOptions = Array.from({ length: 12 }, (_, i) => ({ value: String(i + 1), label: t('cron.schedule.monthOption', { month: i + 1 }) }));
  // "每年"选中具体月份后，日期字段的候选天数必须跟着这个月封顶，否则像"2月31日"这种组合会被
  // 允许选中——croniter 里 day=31 和 month=2 永远不会同时命中，任务实际上永远不会触发，
  // 是个"看起来配置成功、实际静默失效"的坑，不只是下拉框太长那种纯 UI 问题。
  // 2月按平年 28 天封顶（不放到 29）：闰年才会命中的"每年"排班会让用户以为每年都执行，
  // 三年不触发一次同样是隐性失效，跟"每月"用独立的 'L' 选项处理月末是两回事，这里干脆不引入歧义。
  const YEARLY_MONTH_DAY_COUNTS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  const yearlyMaxDay = schedule.kind === 'yearly' && schedule.month ? YEARLY_MONTH_DAY_COUNTS[schedule.month - 1] : 31;
  const yearlyDayOptions = dayOptions.filter((o) => o.value !== 'L' && Number(o.value) <= yearlyMaxDay);
  const periodKindOptions = PERIOD_KINDS.map((k) => ({ value: k, label: t(`cron.schedule.${k}`) }));
  const weekOfMonthOptions = WEEK_OF_MONTH_OPTIONS.map((o) => ({ value: o.value, label: t(`cron.schedule.weekOfMonth.${o.key}`) }));

  return (
    <div className="relative" data-testid="cron-schedule-editor">
      <div className="mb-2 flex items-center gap-1.5 text-sm font-bold text-text-strong">
        <span data-testid="cron-schedule-title">{t('cron.schedule.title')}</span> <span className="text-danger">*</span>
        <span
          className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-border text-[10px] font-normal text-text-muted cursor-help"
          title={t('cron.schedule.help') ?? undefined}
          data-testid="cron-schedule-help"
        >
          ?
        </span>
      </div>

      <div className="mb-3 inline-flex rounded-md bg-bg-muted p-0.5" data-testid="cron-schedule-mode-tabs">
        {(['period', 'interval', 'once'] as const).map((mode) => (
          <button
            key={mode}
            type="button"
            onClick={() => switchTopMode(mode)}
            data-testid={`cron-schedule-mode-tab-${mode}`}
            className={`rounded px-4 py-1.5 text-sm transition-colors ${
              topMode === mode ? 'bg-card font-bold text-text-strong shadow-sm' : 'text-text-muted hover:text-text'
            }`}
          >
            {t(`cron.schedule.mode${mode === 'period' ? 'Period' : mode === 'interval' ? 'Interval' : 'Once'}`)}
          </button>
        ))}
        <button
          type="button"
          onClick={() => setTopMode('cronExpr')}
          data-testid="cron-schedule-mode-tab-cron-expr"
          className={`rounded px-4 py-1.5 text-sm transition-colors ${
            topMode === 'cronExpr' ? 'bg-card font-bold text-text-strong shadow-sm' : 'text-text-muted hover:text-text'
          }`}
        >
          {t('cron.schedule.modeCronExpr')}
        </button>
      </div>

      {topMode === 'period' && (
        <div className="flex flex-col gap-2">
          <div className="flex flex-nowrap items-center gap-2" data-testid="cron-simple-select-5">
            <SimpleSelect
              value={schedule.kind === 'interval' || schedule.kind === 'once' ? 'daily' : schedule.kind === 'monthlyWeekday' ? 'monthly' : schedule.kind}
              onChange={(v) => setPeriodKind(v as Extract<CronScheduleKind, 'daily' | 'weekly' | 'monthly' | 'yearly'>)}
              options={periodKindOptions}
              className="w-24 shrink-0"
            />

            {/* "每月"的二级切换：按日期（已有）/ 按星期（"每月第几周星期几"，见 plan.md §2.3.8），
                跟周期细分选择器放同一行，节省纵向空间 */}
            {(schedule.kind === 'monthly' || schedule.kind === 'monthlyWeekday') && (
              <div className="inline-flex w-fit shrink-0 rounded-md bg-bg-muted p-0.5" data-testid="cron-schedule-monthly-sub-tabs">
                {(['date', 'week'] as const).map((subMode) => {
                  const active = subMode === 'date' ? schedule.kind === 'monthly' : schedule.kind === 'monthlyWeekday';
                  return (
                    <button
                      key={subMode}
                      type="button"
                      onClick={() => setMonthlySubMode(subMode)}
                      data-testid="cron-schedule-monthly-sub-tab"
                      data-variant={subMode}
                      className={`rounded px-3 py-1 text-xs transition-colors ${
                        active ? 'bg-card font-bold text-text-strong shadow-sm' : 'text-text-muted hover:text-text'
                      }`}
                    >
                      {t(`cron.schedule.monthlySubMode.${subMode}`)}
                    </button>
                  );
                })}
              </div>
            )}

            {schedule.kind === 'weekly' && (
              <>
                <WeekdayPicker selected={schedule.weekdays ?? []} onToggle={toggleWeekday} />
                <TimePicker
                  value={schedule.time ?? ''}
                  onChange={(v) => updateSchedule({ ...schedule, time: v })}
                  className="w-28 shrink-0"
                  align="right"
                  placeholder={t('cron.schedule.selectTime') ?? undefined}
                />
              </>
            )}

            {schedule.kind === 'yearly' && (
              <>
                <SimpleSelect
                  value={schedule.month ? String(schedule.month) : ''}
                  onChange={(v) => {
                    const nextMonth = Number(v);
                    const maxDay = YEARLY_MONTH_DAY_COUNTS[nextMonth - 1];
                    // 已选的日期超出新月份的天数上限时（比如从"1月31日"切到"2月"），跟着收窄到
                    // 该月最后一天，而不是留着一个新月份根本不存在的日期
                    const nextDay = typeof schedule.day === 'number' && schedule.day > maxDay ? maxDay : schedule.day;
                    updateSchedule({ ...schedule, month: nextMonth, day: nextDay });
                  }}
                  options={monthOptions}
                  placeholder={t('cron.schedule.selectMonth') ?? undefined}
                  className="min-w-0 flex-1"
                />
                <SimpleSelect
                  value={schedule.day !== undefined ? String(schedule.day) : ''}
                  onChange={(v) => updateSchedule({ ...schedule, day: Number(v) })}
                  options={yearlyDayOptions}
                  placeholder={t('cron.schedule.selectDay') ?? undefined}
                  className="min-w-0 flex-1"
                />
                <TimePicker
                  value={schedule.time ?? ''}
                  onChange={(v) => updateSchedule({ ...schedule, time: v })}
                  className="min-w-0 flex-1"
                  align="right"
                  placeholder={t('cron.schedule.selectTime') ?? undefined}
                />
              </>
            )}

            {schedule.kind === 'daily' && (
              <TimePicker
                value={schedule.time ?? ''}
                onChange={(v) => updateSchedule({ ...schedule, time: v })}
                className="min-w-0 flex-1"
                placeholder={t('cron.schedule.selectTime') ?? undefined}
              />
            )}
          </div>

          {(schedule.kind === 'monthly' || schedule.kind === 'monthlyWeekday') && (
            <div className="flex flex-nowrap items-center gap-2">
              {schedule.kind === 'monthly' && (
                <>
                  <SimpleSelect
                    value={schedule.day !== undefined ? String(schedule.day) : ''}
                    onChange={(v) => updateSchedule({ ...schedule, day: v === 'L' ? 'L' : Number(v) })}
                    options={dayOptions}
                    placeholder={t('cron.schedule.selectDay') ?? undefined}
                    className="min-w-0 flex-1"
                  />
                  <TimePicker
                    value={schedule.time ?? ''}
                    onChange={(v) => updateSchedule({ ...schedule, time: v })}
                    className="min-w-0 flex-1"
                    align="right"
                    placeholder={t('cron.schedule.selectTime') ?? undefined}
                  />
                </>
              )}

              {schedule.kind === 'monthlyWeekday' && (
                <>
                  <SimpleSelect
                    value={schedule.weekOfMonth !== undefined ? String(schedule.weekOfMonth) : ''}
                    onChange={(v) => updateSchedule({ ...schedule, weekOfMonth: v === 'L' ? 'L' : Number(v) })}
                    options={weekOfMonthOptions}
                    className="w-24 shrink-0"
                  />
                  <WeekdayPicker selected={schedule.weekdays ?? []} onToggle={toggleWeekday} />
                  <TimePicker
                    value={schedule.time ?? ''}
                    onChange={(v) => updateSchedule({ ...schedule, time: v })}
                    className="w-28 shrink-0"
                    align="right"
                    placeholder={t('cron.schedule.selectTime') ?? undefined}
                  />
                </>
              )}
            </div>
          )}
        </div>
      )}

      {topMode === 'interval' && schedule.kind === 'interval' && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-nowrap items-center gap-2">
            <span className="shrink-0 text-sm text-text-muted" data-testid="cron-schedule-every-label">{t('cron.schedule.every')}</span>
            <input
              type="text"
              inputMode="numeric"
              value={intervalNumberText}
              title={t('cron.schedule.integerOnlyHint') ?? undefined}
              data-testid="cron-schedule-interval-input"
              onKeyDown={(e) => {
                // 小时/分钟步长在 croniter 里都只支持正整数：在按键这一刻就挡掉非数字字符，
                // 而不是等 onChange 里再"事后清洗"——之前的实现允许小数点先短暂插入、再被
                // 静默过滤掉，用户敲"0.1"会在无任何提示的情况下被拼接成一个和输入无关的整数
                // （比如"0.1"变成"01"=1，"1.5"变成"15"），完全没有反馈，见 2026-07-23 bugfix（bug002）。
                // 放行：数字键、编辑/导航类控制键、以及 Ctrl/Cmd 组合的编辑快捷键（全选/复制/粘贴/剪切/撤销）。
                if (e.ctrlKey || e.metaKey || e.altKey) return;
                const allowedControlKeys = ['Backspace', 'Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Tab', 'Home', 'End', 'Enter', 'Escape'];
                if (allowedControlKeys.includes(e.key)) return;
                if (!/^\d$/.test(e.key)) e.preventDefault();
              }}
              onPaste={(e) => {
                // 粘贴走的是单独的剪贴板事件，不经过 onKeyDown；同样只保留数字字符，
                // 避免粘贴"0.1"这类字符串被 onChange 静默拼接成无关的整数（见上）。
                e.preventDefault();
                const digitsOnly = e.clipboardData.getData('text').replace(/\D/g, '');
                if (!digitsOnly) return;
                const pasted = normalizeIntegerDigits(digitsOnly);
                setIntervalNumberText(pasted);
                const isMinutes = schedule.intervalUnit === 'minutes';
                const n = Number(pasted);
                updateSchedule(isMinutes ? { ...schedule, everyMinutes: n } : { ...schedule, everyHours: n });
              }}
              onChange={(e) => {
                // onKeyDown/onPaste 已经在源头挡掉了非数字输入，这里的过滤是兜底
                // （比如浏览器自动填充、IME 等不经过 onKeyDown 的输入路径）。
                const digitsOnly = e.target.value.replace(/\D/g, '');
                // 去掉前导零，只保留数值本身的字符串形式（"00"→"0"，"01"→"1"，"010"→"10"）：
                // 用户先敲 "0" 再敲其它数字时（包括之前"敲 0 再敲被拦截的小数点再敲 1"这类路径），
                // 框里不应该一直停留在"01"这种带前导零的形式（见 2026-07-23 bugfix 追加需求，bug002）。
                const raw = normalizeIntegerDigits(digitsOnly);
                setIntervalNumberText(raw);
                const isMinutes = schedule.intervalUnit === 'minutes';
                if (raw === '') {
                  updateSchedule(isMinutes ? { ...schedule, everyMinutes: undefined } : { ...schedule, everyHours: undefined });
                  return;
                }
                const n = Number(raw);
                updateSchedule(isMinutes ? { ...schedule, everyMinutes: n } : { ...schedule, everyHours: n });
              }}
              placeholder={t(schedule.intervalUnit === 'minutes' ? 'cron.schedule.everyMinutesPlaceholder' : 'cron.schedule.everyHoursPlaceholder') ?? undefined}
              className="w-16 shrink-0 rounded-md border border-border bg-card px-2 py-1.5 text-sm text-text outline-none focus:border-accent"
            />
            <div className="inline-flex w-fit shrink-0 rounded-md bg-bg-muted p-0.5" data-testid="cron-schedule-interval-unit-tabs">
              {(['hours', 'minutes'] as const).map((unit) => {
                const active = (schedule.intervalUnit ?? 'hours') === unit;
                return (
                  <button
                    key={unit}
                    type="button"
                    onClick={() => setIntervalUnit(unit)}
                    data-testid="cron-schedule-interval-unit-tab"
                    data-variant={unit}
                    className={`rounded px-1.5 py-0.5 text-xs transition-colors ${
                      active ? 'bg-card font-bold text-text-strong shadow-sm' : 'text-text-muted hover:text-text'
                    }`}
                  >
                    {t(`cron.schedule.intervalUnit.${unit}`)}
                  </button>
                );
              })}
            </div>
            <WeekdayPicker selected={schedule.weekdays ?? []} onToggle={toggleWeekday} />
          </div>
          {/* 间隔数字缺失或 < 1（含用户想输小数被约束成整数后落在 0 的情况）时，直接在输入框下面
              给出常驻提示，不用等用户去悬停确定按钮才知道哪里有问题（见 2026-07-23 bugfix，bug002）。 */}
          {(() => {
            const n = schedule.intervalUnit === 'minutes' ? schedule.everyMinutes : schedule.everyHours;
            return n === undefined || n < 1;
          })() && (
            <p className="text-xs text-danger" data-testid="cron-schedule-interval-hint">{t('cron.schedule.integerOnlyHint')}</p>
          )}
        </div>
      )}

      {topMode === 'once' && schedule.kind === 'once' && (
        <div className="flex flex-nowrap items-center gap-2" data-testid="cron-date-picker-2">
          <TimePicker
            value={schedule.time ?? ''}
            onChange={(v) => updateSchedule({ ...schedule, time: v })}
            className="w-32 shrink-0"
            placeholder={t('cron.schedule.selectTime') ?? undefined}
            minTime={schedule.date === todayStr ? nowTimeStr : undefined}
          />
          <DatePicker
            value={schedule.date ?? ''}
            onChange={(v) => updateSchedule({ ...schedule, date: v })}
            className="min-w-0 flex-1"
            minDate={todayStr}
          />
        </div>
      )}

      {topMode === 'cronExpr' && (
        <div>
          <div className="relative">
            <input
              type="text"
              value={value}
              onChange={(e) => onChange(e.target.value)}
              placeholder={t('cron.schedule.cronExprPlaceholder') ?? undefined}
              data-testid="cron-schedule-cron-expr-input"
              className={`w-full rounded-md border bg-card px-3 py-1.5 pr-8 text-sm text-text outline-none mono ${
                !validation.valid ? 'border-danger' : 'border-border focus:border-accent'
              }`}
            />
            <span
              className="absolute right-2 top-1/2 -translate-y-1/2 text-text-muted hover:text-text cursor-help"
              title={t('cron.placeholders.cron') ?? undefined}
              data-testid="cron-schedule-cron-expr-help"
            >
              <svg width="16" height="16" viewBox="0 0 40 40" xmlns="http://www.w3.org/2000/svg">
                <circle cx="20" cy="20" r="18" fill="transparent" stroke="currentColor" strokeWidth="2" />
                <text x="20" y="22" fontSize="24" fill="currentColor" textAnchor="middle" dominantBaseline="middle">?</text>
              </svg>
            </span>
          </div>
          {!validation.valid && (
            <p className="mt-1 text-xs text-danger" data-testid="cron-schedule-cron-expr-error">{t(validation.error || 'cron.errors.cronFormat')}</p>
          )}
        </div>
      )}

      {/* 提前唤醒：与 cron_expr 同属执行计划；对话创建任务常带 300s（5 分钟），面板需可改/可清零 */}
      {onWakeOffsetSecondsChange && (
        <div className="mt-3">
          <div className="mb-1.5 flex items-center gap-1.5 text-sm font-bold text-text-strong">
            <span data-testid="cron-schedule-wake-offset-label">{t('cron.schedule.wakeOffset')}</span>
            <span
              className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-border text-[10px] font-normal text-text-muted cursor-help"
              title={t('cron.schedule.wakeOffsetHelp') ?? undefined}
              data-testid="cron-schedule-wake-offset-help"
            >
              ?
            </span>
          </div>
          <div className="flex flex-nowrap items-center gap-2">
            <input
              type="text"
              inputMode="numeric"
              value={wakeOffsetMinutesText}
              disabled={wakeOffsetDisabled}
              title={wakeOffsetDisabled ? (t('cron.autoManagedToggleDisabled') ?? undefined) : undefined}
              onChange={(e) => {
                const normalized = normalizeWakeOffsetMinutesInput(e.target.value);
                setWakeOffsetMinutesText(normalized);
                onWakeOffsetSecondsChange(wakeOffsetMinutesToSeconds(normalized));
              }}
              placeholder="0"
              data-testid="cron-schedule-wake-offset-input"
              className="w-28 shrink-0 rounded-md border border-border bg-card px-3 py-1.5 text-sm text-text outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-50"
            />
            <span className="shrink-0 text-sm text-text-muted" data-testid="cron-schedule-wake-offset-unit-label">{t('cron.schedule.wakeOffsetUnit')}</span>
          </div>
        </div>
      )}
    </div>
  );
}
