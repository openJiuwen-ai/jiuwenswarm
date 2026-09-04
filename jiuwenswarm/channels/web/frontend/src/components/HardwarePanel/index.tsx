/**
 * HardwarePanel — 硬件管理面板（当前为 NPU 监控）。
 *
 * 数据来源：web RPC `hardware.npu.status`（后端执行 npu-smi info 并解析）。
 * 进入页面自动查询，之后每 5 秒轮询；切换导航卸载组件时停止轮询。
 * 非昇腾环境（无 npu-smi）时后端返回 available=false，面板降级为原因提示。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';

const POLL_INTERVAL_MS = 5000;

interface NpuInfo {
  id: number;
  name: string | null;
  health: string | null;
  powerW: number | null;
  tempC: number | null;
  aicorePercent: number | null;
  memoryUsedMb: number | null;
  memoryTotalMb: number | null;
  busId: string | null;
}

interface NpuStatusResponse {
  available?: boolean;
  reason?: string;
  npus?: unknown[];
}

function toNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function toNpuInfos(raw: unknown[]): NpuInfo[] {
  const rows: NpuInfo[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    if (typeof rec.id !== 'number') continue;
    rows.push({
      id: rec.id,
      name: typeof rec.name === 'string' ? rec.name : null,
      health: typeof rec.health === 'string' ? rec.health : null,
      powerW: toNumber(rec.power_w),
      tempC: toNumber(rec.temp_c),
      aicorePercent: toNumber(rec.aicore_percent),
      memoryUsedMb: toNumber(rec.memory_used_mb),
      memoryTotalMb: toNumber(rec.memory_total_mb),
      busId: typeof rec.bus_id === 'string' ? rec.bus_id : null,
    });
  }
  return rows.sort((a, b) => a.id - b.id);
}

function UsageBar({ percent }: { percent: number | null }) {
  const value = percent === null ? 0 : Math.min(100, Math.max(0, percent));
  const color = percent === null ? 'bg-text-muted' : value >= 85 ? 'bg-red-500' : 'bg-accent';
  return (
    <div className="h-1.5 w-full rounded-full bg-bg-hover">
      <div className={`h-1.5 rounded-full ${color}`} style={{ width: `${value}%` }} />
    </div>
  );
}

export function HardwarePanel() {
  const { t } = useTranslation();
  const [npus, setNpus] = useState<NpuInfo[]>([]);
  const [available, setAvailable] = useState(true);
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const resp = await webRequest<NpuStatusResponse>('hardware.npu.status', {});
      setAvailable(resp?.available !== false);
      setReason(typeof resp?.reason === 'string' ? resp.reason : '');
      setNpus(toNpuInfos(resp?.npus ?? []));
      setUpdatedAt(new Date());
    } catch (err) {
      setAvailable(false);
      setReason(err instanceof Error ? err.message : String(err));
      setNpus([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    timerRef.current = window.setInterval(() => void load(), POLL_INTERVAL_MS);
    return () => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [load]);

  return (
    <div className="flex-1 min-h-0 relative overflow-y-auto" data-testid="hardware-panel">
      <div className="mx-auto max-w-5xl px-6 py-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text">{t('hardware.title')}</h2>
          <div className="flex items-center gap-3">
            {updatedAt && (
              <span className="text-xs text-text-muted">
                {t('hardware.lastUpdated')}: {updatedAt.toLocaleTimeString()}
              </span>
            )}
            <button
              type="button"
              onClick={() => void load()}
              className="rounded-md border border-border bg-card px-3 py-1.5 text-sm text-text hover:bg-bg-hover"
            >
              {t('hardware.refresh')}
            </button>
          </div>
        </div>

        {loading && npus.length === 0 && available && (
          <div className="mt-10 text-center text-sm text-text-muted">{t('hardware.loading')}</div>
        )}

        {!available && (
          <div className="mt-10 rounded-lg border border-dashed border-border px-6 py-10 text-center">
            <div className="text-sm text-text-muted">{t('hardware.unavailable')}</div>
            {reason && <div className="mt-2 text-xs text-text-muted">{reason}</div>}
          </div>
        )}

        {available && !loading && npus.length === 0 && (
          <div className="mt-10 text-center text-sm text-text-muted">{t('hardware.empty')}</div>
        )}

        {npus.length > 0 && (
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {npus.map((npu) => (
              <div
                key={npu.id}
                className="rounded-xl border border-border bg-card p-4"
                data-testid={`npu-card-${npu.id}`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-base font-medium text-text">
                    NPU {npu.id}
                    {npu.name ? ` · ${npu.name}` : ''}
                  </span>
                  {npu.health && (
                    <span
                      className={`inline-flex items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-xs ${
                        npu.health.toUpperCase() === 'OK' ? 'text-ok' : 'text-warn'
                      }`}
                    >
                      <span
                        className={`inline-block h-2 w-2 rounded-full ${
                          npu.health.toUpperCase() === 'OK' ? 'bg-ok' : 'bg-warn'
                        }`}
                      />
                      {npu.health}
                    </span>
                  )}
                </div>

                <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <div className="text-xs text-text-muted">{t('hardware.temp')}</div>
                    <div className="mt-0.5 text-text">
                      {npu.tempC === null ? '-' : `${npu.tempC} °C`}
                    </div>
                  </div>
                  <div>
                    <div className="text-xs text-text-muted">{t('hardware.power')}</div>
                    <div className="mt-0.5 text-text">
                      {npu.powerW === null ? '-' : `${npu.powerW} W`}
                    </div>
                  </div>
                </div>

                <div className="mt-3">
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-text-muted">{t('hardware.aicore')}</span>
                    <span className="text-text">
                      {npu.aicorePercent === null ? '-' : `${npu.aicorePercent}%`}
                    </span>
                  </div>
                  <div className="mt-1">
                    <UsageBar percent={npu.aicorePercent} />
                  </div>
                </div>

                <div className="mt-3">
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-text-muted">{t('hardware.memory')}</span>
                    <span className="text-text">
                      {npu.memoryUsedMb === null || npu.memoryTotalMb === null
                        ? '-'
                        : `${npu.memoryUsedMb} / ${npu.memoryTotalMb} MB`}
                    </span>
                  </div>
                  <div className="mt-1">
                    <UsageBar
                      percent={
                        npu.memoryUsedMb === null || !npu.memoryTotalMb
                          ? null
                          : (npu.memoryUsedMb / npu.memoryTotalMb) * 100
                      }
                    />
                  </div>
                </div>

                {npu.busId && (
                  <div className="mono mt-3 text-xs text-text-muted">{npu.busId}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default HardwarePanel;
