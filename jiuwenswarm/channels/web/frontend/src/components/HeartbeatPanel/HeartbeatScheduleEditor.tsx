import { useTranslation } from 'react-i18next';
import type { HeartbeatScheduleFormValue } from './heartbeatScheduleConvert';
import type { HeartbeatScheduleKind } from '../../types/heartbeat';
import { validateHeartbeatCronExpr } from './heartbeatCronValidation';
import { TIMEZONE_OPTIONS } from '../CronPanel/constants';
import SimpleSelect from '../CronPanel/SimpleSelect';
import DatePicker from '../CronPanel/DatePicker';
import TimePicker from '../CronPanel/TimePicker';
import { nowWallClock } from '../CronPanel/scheduleConvert';

interface HeartbeatScheduleEditorProps {
  value: HeartbeatScheduleFormValue;
  onChange: (value: HeartbeatScheduleFormValue) => void;
  /** 来自 heartbeat.job.meta 的 limits.min_interval_seconds，缺省 60 */
  minIntervalSeconds: number;
}

const KIND_TABS: HeartbeatScheduleKind[] = ['interval', 'cron', 'once'];
const TIMEZONE_SELECT_OPTIONS = TIMEZONE_OPTIONS.map((tz) => ({ value: tz, label: tz }));

export default function HeartbeatScheduleEditor({ value, onChange, minIntervalSeconds }: HeartbeatScheduleEditorProps) {
  const { t } = useTranslation();
  const minIntervalMinutes = Math.max(1, Math.ceil(minIntervalSeconds / 60));
  const intervalMinutes = Math.max(minIntervalMinutes, Math.round(value.intervalSeconds / 60));
  const cronError =
    value.kind === 'cron' && value.cronExpr.trim() ? validateHeartbeatCronExpr(value.cronExpr).error : undefined;
  // "单次"用：今天之前的日期不可选；选中日期正好是今天时，当前时刻之前的时间点也不可选。
  // 与 CronPanel 单次排班同一套做法（nowWallClock 按表单时区取当前墙钟）。
  const nowStr = nowWallClock(value.timezone);
  const todayStr = nowStr.slice(0, 10);
  const nowTimeStr = nowStr.slice(11, 16);

  return (
    <div className="space-y-3" data-testid="heartbeat-panel-schedule-editor">
      <div className="flex gap-2" data-testid="heartbeat-panel-schedule-tab-group">
        {KIND_TABS.map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => onChange({ ...value, kind })}
            className={`rounded-full px-3 py-1 text-sm ${
              value.kind === kind ? 'bg-cron-action font-bold text-cron-action-foreground' : 'border border-border text-text'
            }`}
            data-testid="heartbeat-panel-schedule-tab"
            data-variant={kind}
          >
            {t(`heartbeat.schedule.tab.${kind}`)}
          </button>
        ))}
      </div>

      {value.kind === 'interval' && (
        <div className="flex items-center gap-2" data-testid="heartbeat-panel-interval-row">
          <span className="text-sm text-text" data-testid="heartbeat-panel-interval-label">{t('heartbeat.schedule.interval.label')}</span>
          <input
            type="number"
            min={minIntervalMinutes}
            value={intervalMinutes}
            onChange={(e) => {
              const minutes = Math.max(minIntervalMinutes, Math.floor(Number(e.target.value) || minIntervalMinutes));
              onChange({ ...value, intervalSeconds: minutes * 60 });
            }}
            className="w-24 rounded-md border border-border bg-card px-2 py-1 text-sm"
            data-testid="heartbeat-panel-interval-input"
          />
          <span className="text-sm text-text-muted" data-testid="heartbeat-panel-interval-unit">{t('heartbeat.schedule.interval.unit')}</span>
        </div>
      )}

      {value.kind === 'cron' && (
        <div className="space-y-2" data-testid="heartbeat-panel-cron-row">
          <input
            type="text"
            placeholder="0 0 9 * * ? *"
            value={value.cronExpr}
            onChange={(e) => onChange({ ...value, cronExpr: e.target.value })}
            title={t('heartbeat.schedule.cron.hint') ?? undefined}
            className="w-full rounded-md border border-border bg-card px-2 py-1 text-sm font-mono"
            data-testid="heartbeat-panel-cron-input"
          />
          {cronError && <p className="text-xs text-red-500" data-testid="heartbeat-panel-cron-error">{t(cronError)}</p>}
          <SimpleSelect
            value={value.timezone}
            onChange={(v) => onChange({ ...value, timezone: v })}
            options={TIMEZONE_SELECT_OPTIONS}
            className="w-48"
          />
        </div>
      )}

      {value.kind === 'once' && (
        <div className="flex items-start gap-2" title={t('heartbeat.schedule.once.hint') ?? undefined} data-testid="heartbeat-panel-once-row">
          {/* 复用 CronPanel 的自绘选择器：原生 <input type="date"> 的占位格式跟随浏览器 locale，
              会渲染成「yyyy/mm/日」中英混排；自绘组件全走 i18n 且支持 minDate/minTime 禁选过期。
              未选择时留白（不传 placeholder），见 bug002 用户确认。
              minDate/minTime 每次渲染按 nowWallClock 重算，父层抽屉每 10s 触发一次重渲染，
              用户停在弹层里等到所选时刻过期时，过期的日期/时分选项会即时变为禁选。 */}
          <div className="min-w-0 flex-1">
            <label className="mb-1 block text-xs text-text-muted" data-testid="heartbeat-panel-once-date-label">
              {t('heartbeat.schedule.once.dateLabel')}
              <span className="text-red-500">*</span>
            </label>
            <DatePicker
              value={value.onceDate}
              onChange={(v) => onChange({ ...value, onceDate: v })}
              minDate={todayStr}
              className="w-full"
            />
          </div>
          <div className="w-32 shrink-0">
            <label className="mb-1 block text-xs text-text-muted" data-testid="heartbeat-panel-once-time-label">
              {t('heartbeat.schedule.once.timeLabel')}
              <span className="text-red-500">*</span>
            </label>
            <TimePicker
              value={value.onceTime}
              onChange={(v) => onChange({ ...value, onceTime: v })}
              // 日期未选时按「今天」处理（minDate 已锁死不能选过去），
              // 否则先开时间下拉、还没选日期时整列时分都不禁选，能选到已经过去的点。
              minTime={!value.onceDate || value.onceDate === todayStr ? nowTimeStr : undefined}
              className="w-full"
            />
          </div>
        </div>
      )}
    </div>
  );
}
