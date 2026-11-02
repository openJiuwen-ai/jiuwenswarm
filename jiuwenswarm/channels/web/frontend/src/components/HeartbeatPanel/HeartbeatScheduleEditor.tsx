import { useTranslation } from 'react-i18next';
import type { HeartbeatScheduleFormValue } from './heartbeatScheduleConvert';
import type { HeartbeatScheduleKind } from '../../types/heartbeat';
import { validateHeartbeatCronExpr } from './heartbeatCronValidation';
import { TIMEZONE_OPTIONS } from '../CronPanel/constants';
import SimpleSelect from '../CronPanel/SimpleSelect';

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

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        {KIND_TABS.map((kind) => (
          <button
            key={kind}
            type="button"
            onClick={() => onChange({ ...value, kind })}
            className={`rounded-full px-3 py-1 text-sm ${
              value.kind === kind ? 'bg-cron-action font-bold text-cron-action-foreground' : 'border border-border text-text'
            }`}
          >
            {t(`heartbeat.schedule.tab.${kind}`)}
          </button>
        ))}
      </div>

      {value.kind === 'interval' && (
        <div className="flex items-center gap-2">
          <span className="text-sm text-text">{t('heartbeat.schedule.interval.label')}</span>
          <input
            type="number"
            min={minIntervalMinutes}
            value={intervalMinutes}
            onChange={(e) => {
              const minutes = Math.max(minIntervalMinutes, Math.floor(Number(e.target.value) || minIntervalMinutes));
              onChange({ ...value, intervalSeconds: minutes * 60 });
            }}
            className="w-24 rounded-md border border-border bg-card px-2 py-1 text-sm"
          />
          <span className="text-sm text-text-muted">{t('heartbeat.schedule.interval.unit')}</span>
        </div>
      )}

      {value.kind === 'cron' && (
        <div className="space-y-2">
          <input
            type="text"
            placeholder="0 9 * * 1-5"
            value={value.cronExpr}
            onChange={(e) => onChange({ ...value, cronExpr: e.target.value })}
            title={t('heartbeat.schedule.cron.hint') ?? undefined}
            className="w-full rounded-md border border-border bg-card px-2 py-1 text-sm font-mono"
          />
          {cronError && <p className="text-xs text-red-500">{t(cronError)}</p>}
          <SimpleSelect
            value={value.timezone}
            onChange={(v) => onChange({ ...value, timezone: v })}
            options={TIMEZONE_SELECT_OPTIONS}
            className="w-48"
          />
        </div>
      )}

      {value.kind === 'once' && (
        <div className="flex items-center gap-2" title={t('heartbeat.schedule.once.hint') ?? undefined}>
          <input
            type="date"
            value={value.onceDate}
            onChange={(e) => onChange({ ...value, onceDate: e.target.value })}
            className="rounded-md border border-border bg-card px-2 py-1 text-sm"
          />
          <input
            type="time"
            value={value.onceTime}
            onChange={(e) => onChange({ ...value, onceTime: e.target.value })}
            className="rounded-md border border-border bg-card px-2 py-1 text-sm"
          />
        </div>
      )}
    </div>
  );
}
