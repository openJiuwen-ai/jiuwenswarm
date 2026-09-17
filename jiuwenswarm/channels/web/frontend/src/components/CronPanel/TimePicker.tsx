import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Clock } from 'lucide-react';
import { useClickOutside } from './useClickOutside';

const HOURS = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'));
const MINUTES = Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0'));

interface TimePickerProps {
  value: string; // "HH:MM"，空字符串表示未选择
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
  /** 下拉弹层对齐方向：触发按钮靠近容器右边缘时用 'right'，避免弹层溢出 */
  align?: 'left' | 'right';
  /** 最早可选时间 "HH:MM"（含），早于它的时/分选项禁用点选；不传则不限制。
   * 目前只有"单次"排班在选中日期=今天时会传（见 ScheduleEditor.tsx），其余模式代表的是
   * 循环触发的钟点，不存在"过期"概念。 */
  minTime?: string;
}

// 原生 <input type="time"> 会自带一个浏览器时钟图标，和自绘的 Clock 图标叠在一起变成"两个时钟"，
// 改成完全自绘的 时/分 两列选择器。宽度由调用方通过 className 控制。
export default function TimePicker({ value, onChange, placeholder, className = 'w-32 shrink-0', align = 'left', minTime }: TimePickerProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  useClickOutside(rootRef, open, () => setOpen(false));

  const [h, m] = value ? value.split(':') : ['', ''];
  const [minHour, minMinute] = minTime ? minTime.split(':') : [undefined, undefined];

  function pick(hour: string, minute: string) {
    onChange(`${hour}:${minute}`);
  }

  return (
    <div className={`relative ${className}`} ref={rootRef}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-nowrap items-center justify-between gap-1 rounded-md border border-border bg-card px-3 py-1.5 text-sm outline-none hover:border-border-strong"
      >
        <span className={`truncate ${value ? 'text-text' : 'text-text-muted'}`}>{value || placeholder}</span>
        <Clock size={15} className="shrink-0 text-text-muted" />
      </button>
      {open && (
        <div className={`absolute top-[calc(100%+4px)] z-30 flex w-40 overflow-hidden rounded-lg border border-border bg-card shadow-lg ${align === 'right' ? 'right-0' : 'left-0'}`}>
          <div className="flex-1 max-h-52 overflow-y-auto border-r border-border">
            <div className="sticky top-0 bg-card px-3 py-1.5 text-xs text-text-muted">{t('cron.timePicker.hour')}</div>
            {HOURS.map((hh) => {
              const disabled = minHour !== undefined && hh < minHour;
              return (
                <button
                  key={hh}
                  type="button"
                  disabled={disabled}
                  onClick={() => pick(hh, m || '00')}
                  className={`block w-full px-3 py-1.5 text-left text-sm ${
                    disabled
                      ? 'cursor-not-allowed text-text-muted/50'
                      : hh === h ? 'bg-bg-hover text-text' : 'text-text hover:bg-bg-hover'
                  }`}
                >
                  {hh}
                </button>
              );
            })}
          </div>
          <div className="flex-1 max-h-52 overflow-y-auto">
            <div className="sticky top-0 bg-card px-3 py-1.5 text-xs text-text-muted">{t('cron.timePicker.minute')}</div>
            {MINUTES.map((mm) => {
              const disabled = minHour !== undefined && minMinute !== undefined && h === minHour && mm < minMinute;
              return (
                <button
                  key={mm}
                  type="button"
                  disabled={disabled}
                  onClick={() => pick(h || '00', mm)}
                  className={`block w-full px-3 py-1.5 text-left text-sm ${
                    disabled
                      ? 'cursor-not-allowed text-text-muted/50'
                      : mm === m ? 'bg-bg-hover text-text' : 'text-text hover:bg-bg-hover'
                  }`}
                >
                  {mm}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
