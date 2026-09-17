import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Calendar, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';
import { useClickOutside } from './useClickOutside';

interface DatePickerProps {
  value: string; // "YYYY-MM-DD"，空字符串表示未选择
  onChange: (v: string) => void;
  placeholder?: string;
  className?: string;
  /** 最早可选日期 "YYYY-MM-DD"（含），早于它的日期禁用点选、手动输入也会被拒绝；不传则不限制。 */
  minDate?: string;
}

function buildMonthList(centerYear: number, centerMonth: number): { year: number; month: number }[] {
  const list = [];
  for (let offset = -5; offset <= 6; offset++) {
    const d = new Date(centerYear, centerMonth + offset, 1);
    list.push({ year: d.getFullYear(), month: d.getMonth() });
  }
  return list;
}

// 支持 "2026-5-25" "2026/5/25" 等宽松分隔符与不补零写法，统一归一化为 YYYY-MM-DD
function parseFlexibleDate(raw: string): string | null {
  const m = raw.trim().match(/^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/);
  if (!m) return null;
  const [, y, mo, d] = m;
  const date = new Date(Number(y), Number(mo) - 1, Number(d));
  if (date.getFullYear() !== Number(y) || date.getMonth() !== Number(mo) - 1 || date.getDate() !== Number(d)) return null;
  return `${y}-${mo.padStart(2, '0')}-${d.padStart(2, '0')}`;
}

const WEEKDAY_KEYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];

export default function DatePicker({ value, onChange, placeholder, className = 'flex-1 min-w-0', minDate }: DatePickerProps) {
  const { t } = useTranslation();
  const initial = value ? new Date(value) : new Date();
  const [open, setOpen] = useState(false);
  const [panelView, setPanelView] = useState<'day' | 'year'>('day');
  const [cursor, setCursor] = useState({ year: initial.getFullYear(), month: initial.getMonth() });
  const [yearPageStart, setYearPageStart] = useState(initial.getFullYear() - 5);
  const [text, setText] = useState(value);
  const rootRef = useRef<HTMLDivElement>(null);
  useClickOutside(rootRef, open, () => {
    setOpen(false);
    setPanelView('day');
  });

  useEffect(() => {
    setText(value);
  }, [value]);

  function commitText(raw: string) {
    const parsed = parseFlexibleDate(raw);
    if (parsed && !(minDate && parsed < minDate)) {
      onChange(parsed);
      const d = new Date(parsed);
      setCursor({ year: d.getFullYear(), month: d.getMonth() });
      setText(parsed);
    } else {
      setText(value);
    }
  }

  const first = new Date(cursor.year, cursor.month, 1);
  const startWeekday = first.getDay();
  const daysInMonth = new Date(cursor.year, cursor.month + 1, 0).getDate();
  const cells: (number | null)[] = [...Array(startWeekday).fill(null), ...Array.from({ length: daysInMonth }, (_, i) => i + 1)];

  const selectedDate = value ? new Date(value) : null;
  const monthList = buildMonthList(cursor.year, cursor.month);

  function pick(day: number) {
    const mm = String(cursor.month + 1).padStart(2, '0');
    const dd = String(day).padStart(2, '0');
    onChange(`${cursor.year}-${mm}-${dd}`);
    setOpen(false);
  }

  function openYearPicker() {
    setYearPageStart(cursor.year - 5);
    setPanelView('year');
  }

  function pickYear(year: number) {
    setCursor((c) => ({ ...c, year }));
    setPanelView('day');
  }

  return (
    <div className={`relative ${className}`} ref={rootRef}>
      <div
        className="flex w-full items-center gap-1.5 rounded-md border border-border bg-card px-3 py-1.5 text-sm focus-within:border-accent"
        onClick={() => setOpen(true)}
      >
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onFocus={() => setOpen(true)}
          onBlur={() => commitText(text)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              commitText(text);
              setOpen(false);
            }
          }}
          placeholder={placeholder}
          className="min-w-0 flex-1 bg-transparent text-text outline-none placeholder:text-text-muted"
        />
        <Calendar
          size={15}
          className="shrink-0 cursor-pointer text-text-muted"
          onClick={(e) => {
            e.stopPropagation();
            setOpen((v) => !v);
          }}
        />
      </div>
      {open && panelView === 'day' && (
        <div className="absolute left-0 top-[calc(100%+4px)] z-30 flex w-96 overflow-hidden rounded-lg border border-border bg-card shadow-lg">
          <div className="w-16 shrink-0 max-h-72 overflow-y-auto border-r border-border py-1">
            {monthList.map((m, idx) => (
              <div key={`${m.year}-${m.month}`}>
                {(idx === 0 || m.year !== monthList[idx - 1].year) && (
                  <div className="px-2 pt-2 pb-1 text-xs font-bold text-text-muted">{m.year}</div>
                )}
                <button
                  type="button"
                  onClick={() => setCursor({ year: m.year, month: m.month })}
                  className={`block w-full px-2 py-1.5 text-left text-sm ${
                    m.year === cursor.year && m.month === cursor.month
                      ? 'bg-bg-hover font-bold text-text'
                      : 'text-text hover:bg-bg-hover'
                  }`}
                >
                  {t('cron.datePicker.monthItem', { month: m.month + 1 })}
                </button>
              </div>
            ))}
          </div>
          <div className="flex-1 p-4">
            <div className="mb-3 flex items-center justify-between">
              <div className="flex items-center">
                <button
                  type="button"
                  onClick={() => setCursor((c) => ({ ...c, year: c.year - 1 }))}
                  title={t('cron.datePicker.prevYear') ?? undefined}
                  className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text"
                >
                  <ChevronsLeft size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => setCursor((c) => (c.month === 0 ? { year: c.year - 1, month: 11 } : { year: c.year, month: c.month - 1 }))}
                  title={t('cron.datePicker.prevMonth') ?? undefined}
                  className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text"
                >
                  <ChevronLeft size={18} />
                </button>
              </div>
              <button
                type="button"
                onClick={openYearPicker}
                className="rounded px-2 py-1 text-base font-medium text-text hover:bg-bg-hover"
              >
                {t('cron.datePicker.yearMonthHeader', { year: cursor.year, month: cursor.month + 1 })}
              </button>
              <div className="flex items-center">
                <button
                  type="button"
                  onClick={() => setCursor((c) => (c.month === 11 ? { year: c.year + 1, month: 0 } : { year: c.year, month: c.month + 1 }))}
                  title={t('cron.datePicker.nextMonth') ?? undefined}
                  className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text"
                >
                  <ChevronRight size={18} />
                </button>
                <button
                  type="button"
                  onClick={() => setCursor((c) => ({ ...c, year: c.year + 1 }))}
                  title={t('cron.datePicker.nextYear') ?? undefined}
                  className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text"
                >
                  <ChevronsRight size={18} />
                </button>
              </div>
            </div>
            <div className="grid grid-cols-7 gap-1 text-center text-sm text-text-muted mb-1.5">
              {WEEKDAY_KEYS.map((key) => (
                <span key={key}>{t(`cron.datePicker.weekday.${key}`)}</span>
              ))}
            </div>
            <div className="grid grid-cols-7 gap-1">
              {cells.map((day, idx) => {
                if (day === null) return <span key={idx} />;
                const isSelected =
                  selectedDate &&
                  selectedDate.getFullYear() === cursor.year &&
                  selectedDate.getMonth() === cursor.month &&
                  selectedDate.getDate() === day;
                const cellDateStr = `${cursor.year}-${String(cursor.month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
                const disabled = Boolean(minDate && cellDateStr < minDate);
                return (
                  <button
                    key={idx}
                    type="button"
                    disabled={disabled}
                    onClick={() => pick(day)}
                    className={`h-9 w-9 rounded-full text-sm transition-colors ${
                      disabled
                        ? 'cursor-not-allowed text-text-muted/40'
                        : isSelected ? 'bg-accent text-accent-foreground' : 'text-text hover:bg-bg-hover'
                    }`}
                  >
                    {day}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}
      {open && panelView === 'year' && (
        <div className="absolute left-0 top-[calc(100%+4px)] z-30 w-72 rounded-lg border border-border bg-card p-4 shadow-lg">
          <div className="mb-3 flex items-center justify-between">
            <button
              type="button"
              onClick={() => setYearPageStart((y) => y - 12)}
              className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text"
            >
              <ChevronLeft size={18} />
            </button>
            <span className="text-base font-medium text-text">{yearPageStart} - {yearPageStart + 11}</span>
            <button
              type="button"
              onClick={() => setYearPageStart((y) => y + 12)}
              className="rounded p-1 text-text-muted hover:bg-bg-hover hover:text-text"
            >
              <ChevronRight size={18} />
            </button>
          </div>
          <div className="grid grid-cols-3 gap-2">
            {Array.from({ length: 12 }, (_, i) => yearPageStart + i).map((y) => (
              <button
                key={y}
                type="button"
                onClick={() => pickYear(y)}
                className={`rounded-md py-2 text-sm transition-colors ${
                  y === cursor.year ? 'bg-accent text-accent-foreground' : 'text-text hover:bg-bg-hover'
                }`}
              >
                {y}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
