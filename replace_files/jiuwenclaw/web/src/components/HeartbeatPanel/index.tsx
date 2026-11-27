import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileViewer } from '../AgentPanel/FileViewer';
import { HeartbeatMessageModal } from '../../features/HeartbeatMessageModal';
import { webRequest } from '../../services/webClient';
import { useSessionStore } from '../../stores';

const HEARTBEAT_FILE_NAME = 'HEARTBEAT.md';

interface ActiveHours {
  start: string;
  end: string;
}

interface HeartbeatConf {
  every: number;
  target: string;
  active_hours: ActiveHours | null;
}

const DEFAULT_CONF: HeartbeatConf = {
  every: 60,
  target: 'web',
  active_hours: null,
};

function normalizeHeartbeatConf(input: unknown): HeartbeatConf {
  if (!input || typeof input !== 'object') {
    return DEFAULT_CONF;
  }
  const data = input as Record<string, unknown>;
  const everyRaw = Number(data.every);
  const every = Number.isFinite(everyRaw) && everyRaw > 0 ? everyRaw : DEFAULT_CONF.every;
  const target = typeof data.target === 'string' ? data.target : DEFAULT_CONF.target;
  const activeHoursRaw = data.active_hours;
  let activeHours: ActiveHours | null = null;
  if (activeHoursRaw && typeof activeHoursRaw === 'object') {
    const active = activeHoursRaw as Record<string, unknown>;
    if (typeof active.start === 'string' && typeof active.end === 'string') {
      activeHours = { start: active.start, end: active.end };
    }
  }
  return {
    every,
    target,
    active_hours: activeHours,
  };
}

function isValidTime(text: string): boolean {
  const match = /^(\d{2}):(\d{2})$/.exec(text.trim());
  if (!match) {
    return false;
  }
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  return hour >= 0 && hour <= 23 && minute >= 0 && minute <= 59;
}

export function HeartbeatPanel() {
  const { t, i18n } = useTranslation();
  const { isConnected, heartbeatState, heartbeatMessage, heartbeatUpdatedAt, heartbeatHistory } = useSessionStore();
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMessage, setModalMessage] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [conf, setConf] = useState<HeartbeatConf>(DEFAULT_CONF);
  const [everyInput, setEveryInput] = useState<string>(String(DEFAULT_CONF.every));
  const [targetInput, setTargetInput] = useState<string>(DEFAULT_CONF.target);
  const [activeHoursEnabled, setActiveHoursEnabled] = useState(false);
  const [startInput, setStartInput] = useState('08:00');
  const [endInput, setEndInput] = useState('22:00');
  const [filePath, setFilePath] = useState<string>('');

  const loadConf = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSuccess(null);
    try {
      const payload = await webRequest<Record<string, unknown>>('heartbeat.get_conf');
      const normalized = normalizeHeartbeatConf(payload);
      setConf(normalized);
      setEveryInput(String(normalized.every));
      setTargetInput(normalized.target);
      if (normalized.active_hours) {
        setActiveHoursEnabled(true);
        setStartInput(normalized.active_hours.start);
        setEndInput(normalized.active_hours.end);
      } else {
        setActiveHoursEnabled(false);
      }
    } catch (loadError) {
      const message = loadError instanceof Error ? loadError.message : t('heartbeat.errors.loadConfig');
      setError(message);
    } finally {
      setLoading(false);
    }
  }, [t]);

  const loadFilePath = useCallback(async () => {
    try {
      const payload = await webRequest<{ path: string }>('heartbeat.get_path');
      setFilePath(payload.path);
    } catch (err) {
      console.error('Failed to load heartbeat file path:', err);
    }
  }, []);

  useEffect(() => {
    void loadConf();
    void loadFilePath();
  }, [loadConf, loadFilePath]);

  useEffect(() => {
    if (!success) {
      return;
    }
    const timer = window.setTimeout(() => {
      setSuccess(null);
    }, 2000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [success]);

  const heartbeatBadge = useMemo(() => {
    if (heartbeatState === 'ok') {
      return {
        text: t('heartbeat.badges.ok'),
        className: 'text-ok border-[var(--border-ok)] bg-ok-subtle',
      };
    }
    if (heartbeatState === 'alert') {
      return {
        text: t('heartbeat.badges.alert'),
        className: 'text-danger border-[var(--border-danger)] bg-danger-subtle',
      };
    }
    return {
      text: t('heartbeat.badges.unknown'),
      className: 'text-text-muted border-border bg-secondary/50',
    };
  }, [heartbeatState, t]);

  const hasChanges = useMemo(() => {
    if (Number(everyInput) !== conf.every) {
      return true;
    }
    if (targetInput !== conf.target) {
      return true;
    }
    if (!!conf.active_hours !== activeHoursEnabled) {
      return true;
    }
    if (activeHoursEnabled) {
      const currentStart = conf.active_hours?.start ?? '';
      const currentEnd = conf.active_hours?.end ?? '';
      if (startInput !== currentStart || endInput !== currentEnd) {
        return true;
      }
    }
    return false;
  }, [activeHoursEnabled, conf, endInput, everyInput, startInput, targetInput]);

  const resetDraft = () => {
    setEveryInput(String(conf.every));
    setTargetInput(conf.target);
    if (conf.active_hours) {
      setActiveHoursEnabled(true);
      setStartInput(conf.active_hours.start);
      setEndInput(conf.active_hours.end);
    } else {
      setActiveHoursEnabled(false);
      setStartInput('08:00');
      setEndInput('22:00');
    }
    setError(null);
    setSuccess(null);
  };

  const saveConf = useCallback(async () => {
    if (saving) {
      return;
    }
    const every = Number(everyInput);
    if (!Number.isFinite(every) || every <= 0) {
      setError(t('heartbeat.errors.invalidEvery'));
      return;
    }
    if (activeHoursEnabled) {
      if (!isValidTime(startInput) || !isValidTime(endInput)) {
        setError(t('heartbeat.errors.invalidActiveHours'));
        return;
      }
      // 验证结束时间大于开始时间
      if (endInput <= startInput) {
        setError(t('heartbeat.errors.invalidRange'));
        return;
      }
    }
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const params: Record<string, unknown> = {
        every,
        target: targetInput,
      };
      if (activeHoursEnabled) {
        params.active_hours = {
          start: startInput.trim(),
          end: endInput.trim(),
        };
      } else {
        // 约定传空对象用于清除 active_hours，表示全天生效
        params.active_hours = {};
      }
      const payload = await webRequest<Record<string, unknown>>('heartbeat.set_conf', params);
      const normalized = normalizeHeartbeatConf(payload);
      setConf(normalized);
      setEveryInput(String(normalized.every));
      setTargetInput(normalized.target);
      if (normalized.active_hours) {
        setActiveHoursEnabled(true);
        setStartInput(normalized.active_hours.start);
        setEndInput(normalized.active_hours.end);
      } else {
        setActiveHoursEnabled(false);
      }
      setSuccess(t('heartbeat.saved'));
    } catch (saveError) {
      const message = saveError instanceof Error ? saveError.message : t('heartbeat.errors.saveConfig');
      setError(message);
    } finally {
      setSaving(false);
    }
  }, [activeHoursEnabled, endInput, everyInput, saving, startInput, t, targetInput]);

  const openMessageModal = useCallback((message: string) => {
    setModalMessage(message);
    setModalOpen(true);
  }, []);

  const closeMessageModal = useCallback(() => {
    setModalOpen(false);
  }, []);

  const getMessagePreview = useCallback((message: string, maxLength = 80) => {
    const normalized = message.replace(/\s+/g, ' ').trim();
    if (normalized.length <= maxLength) {
      return normalized;
    }
    return `${normalized.slice(0, maxLength)}...`;
  }, []);

  return (
    <div className="flex-1 min-h-0 relative">
      <div className="card w-full h-full flex flex-col">
        {success ? (
          <div className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 z-20">
            <div className="bg-ok text-white px-4 py-2 rounded-lg shadow-lg animate-rise text-sm">
              {success}
            </div>
          </div>
        ) : null}
        <div className="flex items-center justify-between gap-4 mb-4">
          <div>
            <h2 className="text-lg font-semibold">{t('heartbeat.title')}</h2>
            <p className="text-sm text-text-muted mt-1">{t('heartbeat.subtitle')}</p>
          </div>
          <div className="flex items-center gap-2">
            <span className={`mono px-2.5 py-1 rounded-full border text-xs ${heartbeatBadge.className}`}>
              {heartbeatBadge.text}
            </span>
          </div>
        </div>

        <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,3fr)_minmax(0,7fr)] gap-4">
          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0">
            <div className="px-4 py-3 bg-secondary/30 border-b border-border">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h3 className="text-sm font-medium text-text">{t('heartbeat.configTitle')}</h3>
                  <p className="text-xs text-text-muted mt-1">{t('heartbeat.configSubtitle')}</p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                    onClick={() => void loadConf()}
                    disabled={saving}
                  >
                    {t('common.refresh')}
                  </button>
                  <button
                    type="button"
                    className="btn !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                    onClick={resetDraft}
                    disabled={loading || saving || !hasChanges}
                  >
                    {t('common.cancel')}
                  </button>
                  <button
                    type="button"
                    className="btn primary !px-3 !py-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
                    onClick={() => void saveConf()}
                    disabled={loading || saving || !hasChanges || !isConnected}
                  >
                    {saving ? t('common.saving') : t('common.save')}
                  </button>
                </div>
              </div>
            </div>
            <div className="flex-1 min-h-0 p-4 flex flex-col gap-3 text-sm text-text-muted">
              {loading ? (
                <div className="rounded-lg border border-border bg-secondary/30 px-3 py-2">
                  {t('heartbeat.loadingConfig')}
                </div>
              ) : null}

              {error ? (
                <div className="rounded-lg border border-danger/30 bg-danger/10 px-3 py-2 text-danger">
                  {error}
                </div>
              ) : null}

              {!loading ? (
                <>
                  <label className="block space-y-1.5">
                    <span className="text-xs uppercase tracking-wide text-text-muted">{t('heartbeat.everyLabel')}</span>
                    <input
                      type="number"
                      min={1}
                      step="1"
                      value={everyInput}
                      onChange={(event) => setEveryInput(event.target.value)}
                      className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                    />
                  </label>

                  <label className="block space-y-1.5">
                    <span className="text-xs uppercase tracking-wide text-text-muted">{t('heartbeat.targetLabel')}</span>
                    <select
                      value={targetInput}
                      onChange={(event) => setTargetInput(event.target.value)}
                      className="w-full rounded-md border border-border bg-bg px-3 py-2 text-[13px] text-text outline-none focus:border-accent"
                    >
                      <option value="web">{t('heartbeat.channels.web')}</option>
                      <option value="feishu">{t('heartbeat.channels.feishu')}</option>
                      <option value="wecom">{t('heartbeat.channels.wecom')}</option>
                      <option value="xiaoyi" disabled style={{ color: '#8c8c96ff'}}>{t('heartbeat.channels.xiaoyi')}</option>
                      <option value="dingtalk" disabled style={{ color: '#8c8c96ff' }}>{t('heartbeat.channels.dingtalk')}</option>
                    </select>
                  </label>

                  <div className="rounded-lg border border-border bg-secondary/20 p-3 space-y-2">
                    <label className="inline-flex items-center gap-2 cursor-pointer select-none">
                      <input
                        type="checkbox"
                        checked={activeHoursEnabled}
                        onChange={(event) => setActiveHoursEnabled(event.target.checked)}
                        className="rounded border-border"
                      />
                      <span className="text-sm text-text">{t('heartbeat.enableActiveHours')}</span>
                    </label>
                    <div className="grid grid-cols-2 gap-2">
                      <label className="space-y-1">
                        <span className="text-xs text-text-muted">{t('heartbeat.startTime')}</span>
                        <input
                          type="time"
                          value={startInput}
                          onChange={(event) => setStartInput(event.target.value)}
                          disabled={!activeHoursEnabled}
                          className={`w-full rounded-md border px-3 py-2 text-[13px] outline-none ${
                            activeHoursEnabled
                              ? 'border-border bg-bg text-text focus:border-accent'
                              : 'border-border bg-secondary/60 text-text-muted cursor-not-allowed'
                          }`}
                        />
                      </label>
                      <label className="space-y-1">
                        <span className="text-xs text-text-muted">{t('heartbeat.endTime')}</span>
                        <input
                          type="time"
                          value={endInput}
                          onChange={(event) => setEndInput(event.target.value)}
                          disabled={!activeHoursEnabled}
                          className={`w-full rounded-md border px-3 py-2 text-[13px] outline-none ${
                            activeHoursEnabled
                              ? 'border-border bg-bg text-text focus:border-accent'
                              : 'border-border bg-secondary/60 text-text-muted cursor-not-allowed'
                          }`}
                        />
                      </label>
                    </div>
                  </div>

                  <div className="rounded-lg border border-border bg-secondary/20 p-3 flex-1 min-h-0 flex flex-col">
                    <div className="flex items-center justify-between mb-2">
                      <p className="text-xs uppercase tracking-wide text-text-muted">{t('heartbeat.recentMessages')}</p>
                      <span className="mono text-[11px] text-text-muted">{heartbeatHistory.length}/20</span>
                    </div>
                    {heartbeatHistory.length === 0 ? (
                      <div className="flex-1 min-h-0 rounded-md border border-border bg-card/40 flex items-center justify-center">
                        <p className="text-xs text-text-muted">{t('heartbeat.noHistory')}</p>
                      </div>
                    ) : (
                      <div className="flex-1 min-h-0 overflow-auto pr-1">
                        {heartbeatHistory.map((item, idx) => (
                          <button
                            type="button"
                            key={`${item.updatedAt}-${idx}`}
                            className="w-full text-left flex items-center gap-3 rounded-md border border-border bg-card/60 px-3 py-2.5 mb-1.5 last:mb-0 hover:bg-card/80 transition-colors"
                            onClick={() => openMessageModal(item.message)}
                          >
                            <span
                              className={`shrink-0 inline-flex h-8 w-12 items-center justify-center rounded-md border text-sm font-bold tracking-wide ${
                                item.status === 'ok'
                                  ? 'text-ok border-[var(--border-ok)] bg-ok-subtle'
                                  : item.status === 'alert'
                                    ? 'text-ok border-[var(--border-ok)] bg-ok-subtle'
                                    : 'text-text-muted border-border bg-secondary/60'
                              }`}
                            >
                              {item.status === 'unknown' ? t('heartbeat.statusUnknown') : t('heartbeat.statusOk')}
                            </span>
                            <div className="min-w-0 flex-1">
                              <p className="text-[13px] text-text leading-snug overflow-hidden text-ellipsis whitespace-nowrap">
                                {getMessagePreview(item.message)}
                              </p>
                              <p className="text-[11px] text-text-muted mt-0.5">{new Date(item.updatedAt).toLocaleString(i18n.language)}</p>
                            </div>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>

                </>
              ) : null}

              <div className="text-xs text-text-muted space-y-1">
                <p>{t('heartbeat.latestTime')}：{heartbeatUpdatedAt ? new Date(heartbeatUpdatedAt).toLocaleString(i18n.language) : t('heartbeat.none')}</p>
                <p
                  className="block w-full max-w-full overflow-hidden text-ellipsis whitespace-nowrap"
                  title={heartbeatMessage ?? 'HEARTBEAT_UNKNOWN'}
                >
                  {t('heartbeat.latestContent')}：{heartbeatMessage ? getMessagePreview(heartbeatMessage) : 'HEARTBEAT_UNKNOWN'}
                </p>
              </div>

            </div>
          </div>

          <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm min-h-0">
            <FileViewer filePath={filePath} fileName={HEARTBEAT_FILE_NAME} />
          </div>
        </div>
      </div>
      <HeartbeatMessageModal
        open={modalOpen}
        message={modalMessage}
        onClose={closeMessageModal}
      />
    </div>
  );
}
