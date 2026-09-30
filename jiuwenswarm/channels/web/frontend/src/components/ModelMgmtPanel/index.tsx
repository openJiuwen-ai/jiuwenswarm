/**
 * ModelMgmtPanel — 模型管理面板（纯展示）。
 *
 * 数据来源:web RPC `model_registry.list`(转发到 agent server 的
 * ModelRegistry,运行状态由后端对 api_base 实时健康检查合成)。
 * 注意不能用 `models.list`——该方法名已被配置面板的本地 handler 占用。模型只由 skill 通过
 * harness 工具 register_model / unregister_model 注册/注销,页面不提供
 * 增删改。
 * 进入页面自动查询,之后每 30 秒轮询;切换导航卸载组件时停止轮询。
 * 后端不可用（如 agent server 未启动）时降级为原因提示。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';

const POLL_INTERVAL_MS = 30000;

interface ModelInfo {
  name: string;
  provider: string | null;
  description: string | null;
  modelType: string;
  protocol: string;
  apiBase: string;
  apiKeyMasked: string | null;
  paramSize: string | null;
  quantization: string | null;
  contextLength: number | null;
  capabilities: string[];
  registeredAt: string | null;
  status: 'running' | 'stopped' | 'unknown';
  latencyMs: number | null;
  lastCheckedAt: string | null;
}

interface ModelsListResponse {
  models?: unknown[];
}

function toStr(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function toNum(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function toModelInfos(raw: unknown[]): ModelInfo[] {
  const rows: ModelInfo[] = [];
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue;
    const rec = item as Record<string, unknown>;
    if (typeof rec.name !== 'string' || !rec.name) continue;
    const status = rec.status;
    rows.push({
      name: rec.name,
      provider: toStr(rec.provider),
      description: toStr(rec.description),
      modelType: toStr(rec.model_type) ?? 'llm',
      protocol: toStr(rec.protocol) ?? 'openai',
      apiBase: toStr(rec.api_base) ?? '',
      apiKeyMasked: toStr(rec.api_key_masked),
      paramSize: toStr(rec.param_size),
      quantization: toStr(rec.quantization),
      contextLength: toNum(rec.context_length),
      capabilities: Array.isArray(rec.capabilities)
        ? rec.capabilities.filter((c): c is string => typeof c === 'string')
        : [],
      registeredAt: toStr(rec.registered_at),
      status: status === 'running' || status === 'stopped' ? status : 'unknown',
      latencyMs: toNum(rec.latency_ms),
      lastCheckedAt: toStr(rec.last_checked_at),
    });
  }
  return rows.sort((a, b) => a.name.localeCompare(b.name));
}

/** 32768 -> "32K" */
function formatContextLength(n: number | null): string {
  if (n === null) return '-';
  if (n >= 1024 && n % 1024 === 0) return `${n / 1024}K`;
  return String(n);
}

function formatTime(iso: string | null): string {
  if (!iso) return '-';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString();
}

export function ModelMgmtPanel() {
  const { t } = useTranslation();
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [loadError, setLoadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const resp = await webRequest<ModelsListResponse>('model_registry.list', {});
      setModels(toModelInfos(resp?.models ?? []));
      setLoadError('');
      setUpdatedAt(new Date());
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
      setModels([]);
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

  const statusLabel = (s: ModelInfo['status']) =>
    s === 'running' ? t('modelmgmt.statusRunning')
      : s === 'stopped' ? t('modelmgmt.statusStopped')
        : t('modelmgmt.statusUnknown');
  const statusColor = (s: ModelInfo['status']) =>
    s === 'running' ? 'text-ok' : s === 'stopped' ? 'text-text-muted' : 'text-warn';
  const statusDot = (s: ModelInfo['status']) =>
    s === 'running' ? 'bg-ok' : s === 'stopped' ? 'bg-text-muted' : 'bg-warn';

  return (
    <div className="flex-1 min-h-0 relative overflow-y-auto" data-testid="modelmgmt-panel">
      <div className="mx-auto max-w-5xl px-6 py-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-text">{t('modelmgmt.title')}</h2>
          <div className="flex items-center gap-3">
            {updatedAt && (
              <span className="text-xs text-text-muted">
                {t('modelmgmt.lastUpdated')}: {updatedAt.toLocaleTimeString()}
              </span>
            )}
            <button
              type="button"
              onClick={() => void load()}
              className="rounded-md border border-border bg-card px-3 py-1.5 text-sm text-text hover:bg-bg-hover"
            >
              {t('modelmgmt.refresh')}
            </button>
          </div>
        </div>

        {loading && models.length === 0 && !loadError && (
          <div className="mt-10 text-center text-sm text-text-muted">{t('modelmgmt.loading')}</div>
        )}

        {loadError && (
          <div className="mt-10 rounded-lg border border-dashed border-border px-6 py-10 text-center">
            <div className="text-sm text-text-muted">{t('modelmgmt.unavailable')}</div>
            <div className="mt-2 text-xs text-text-muted">{loadError}</div>
          </div>
        )}

        {!loadError && !loading && models.length === 0 && (
          <div className="mt-10 rounded-lg border border-dashed border-border px-6 py-10 text-center">
            <div className="text-sm text-text-muted">{t('modelmgmt.empty')}</div>
            <div className="mt-2 text-xs text-text-muted">{t('modelmgmt.emptyHint')}</div>
          </div>
        )}

        {models.length > 0 && (
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {models.map((model) => (
              <div
                key={model.name}
                className="rounded-xl border border-border bg-card p-4"
                data-testid={`model-card-${model.name}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-base font-medium text-text break-all">{model.name}</span>
                  <span
                    className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border px-2 py-0.5 text-xs ${statusColor(model.status)}`}
                  >
                    <span className={`inline-block h-2 w-2 rounded-full ${statusDot(model.status)}`} />
                    {statusLabel(model.status)}
                  </span>
                </div>

                <div className="mt-1 text-xs text-text-muted">
                  {model.modelType.toUpperCase()}
                  {' · '}
                  {model.protocol === 'openai' ? t('modelmgmt.protocolOpenai') : model.protocol}
                  {model.provider ? ` · ${model.provider}` : ''}
                </div>

                {model.description && (
                  <div className="mt-2 text-xs text-text-muted">{model.description}</div>
                )}

                <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <div className="text-xs text-text-muted">{t('modelmgmt.paramSize')}</div>
                    <div className="mt-0.5 text-text">{model.paramSize ?? '-'}</div>
                  </div>
                  <div>
                    <div className="text-xs text-text-muted">{t('modelmgmt.quantization')}</div>
                    <div className="mt-0.5 text-text">{model.quantization ?? '-'}</div>
                  </div>
                  <div>
                    <div className="text-xs text-text-muted">{t('modelmgmt.contextLength')}</div>
                    <div className="mt-0.5 text-text">{formatContextLength(model.contextLength)}</div>
                  </div>
                  <div>
                    <div className="text-xs text-text-muted">{t('modelmgmt.lastChecked')}</div>
                    <div className="mt-0.5 text-text">
                      {model.latencyMs === null ? '-' : `${model.latencyMs} ms`}
                    </div>
                  </div>
                </div>

                {model.apiBase && (
                  <div className="mono mt-3 text-xs text-text-muted break-all">{model.apiBase}</div>
                )}

                {model.apiKeyMasked && (
                  <div className="mono mt-1 text-xs text-text-muted">
                    API Key: {model.apiKeyMasked}
                  </div>
                )}

                {model.capabilities.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {model.capabilities.map((cap) => (
                      <span
                        key={cap}
                        className="rounded-full border border-border px-2 py-0.5 text-xs text-text-muted"
                      >
                        {cap}
                      </span>
                    ))}
                  </div>
                )}

                {model.registeredAt && (
                  <div className="mt-3 text-xs text-text-muted">
                    {t('modelmgmt.registeredAt')}: {formatTime(model.registeredAt)}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default ModelMgmtPanel;
