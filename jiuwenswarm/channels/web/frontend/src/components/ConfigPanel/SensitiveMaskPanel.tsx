import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { webRequest } from '../../services/webClient';

type MaskRule = { title: string; pattern: string };

type MaskConfig = { enabled: boolean; rules: MaskRule[] };

export type SensitiveMaskHandle = {
  dirty: boolean;
  save: () => Promise<void>;
  reset: () => void;
};

type SensitiveMaskPanelProps = {
  isConnected: boolean;
  onDirtyChange: (dirty: boolean) => void;
  handleRef: { current: SensitiveMaskHandle | null };
};

function sameMask(left: MaskConfig, right: MaskConfig): boolean {
  if (left.enabled !== right.enabled || left.rules.length !== right.rules.length) {
    return false;
  }
  return left.rules.every((rule, index) => (
    rule.title === right.rules[index]?.title && rule.pattern === right.rules[index]?.pattern
  ));
}

function cloneMask(config: MaskConfig): MaskConfig {
  return {
    enabled: config.enabled,
    rules: config.rules.map((rule) => ({ title: rule.title, pattern: rule.pattern })),
  };
}

export function SensitiveMaskPanel({ isConnected, onDirtyChange, handleRef }: SensitiveMaskPanelProps) {
  const { t } = useTranslation();
  const [saved, setSaved] = useState<MaskConfig>({ enabled: false, rules: [] });
  const [enabled, setEnabled] = useState(false);
  const [rules, setRules] = useState<MaskRule[]>([]);
  const [loadError, setLoadError] = useState('');

  const applySaved = useCallback((config: MaskConfig) => {
    const next = cloneMask(config);
    setSaved(next);
    setEnabled(next.enabled);
    setRules(next.rules);
  }, []);

  const load = useCallback(async () => {
    if (!isConnected) return;
    try {
      const payload = await webRequest<MaskConfig>('sensitive_mask.get_conf');
      applySaved({
        enabled: Boolean(payload?.enabled),
        rules: Array.isArray(payload?.rules) ? payload.rules.map((item) => ({
          title: String(item?.title ?? ''),
          pattern: String(item?.pattern ?? ''),
        })) : [],
      });
      setLoadError('');
    } catch {
      setLoadError(t('config.sensitiveMask.loadFailed'));
    }
  }, [applySaved, isConnected, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const draft: MaskConfig = { enabled, rules };
  const dirty = !sameMask(draft, saved);

  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  useEffect(() => {
    handleRef.current = {
      dirty,
      save: async () => {
        const payload = await webRequest<MaskConfig>('sensitive_mask.set_conf', {
          enabled,
          rules,
        });
        applySaved({
          enabled: Boolean(payload?.enabled),
          rules: Array.isArray(payload?.rules) ? payload.rules.map((item) => ({
            title: String(item?.title ?? ''),
            pattern: String(item?.pattern ?? ''),
          })) : rules,
        });
      },
      reset: () => {
        setEnabled(saved.enabled);
        setRules(saved.rules.map((rule) => ({ ...rule })));
      },
    };
  }, [applySaved, dirty, enabled, handleRef, rules, saved]);

  const updateRule = (index: number, patch: Partial<MaskRule>) => {
    setRules((current) => current.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...patch } : item
    )));
  };

  return (
    <div
      className="rounded-xl border border-border bg-card/70 overflow-hidden shadow-sm"
      data-testid="config-sensitive-mask"
    >
      <div className="px-4 py-3 bg-secondary/30">
        <span className="text-sm font-medium text-text-strong">{t('config.sensitiveMask.title')}</span>
        <span className="block text-xs text-text-muted mt-0.5">{t('config.sensitiveMask.hint')}</span>
      </div>
      <table className="w-full text-sm border-t border-border">
        <tbody>
          <tr>
            <td className="px-4 py-2.5 align-middle text-xs text-text-muted w-[32%]">
              {t('config.sensitiveMask.enabled')}
            </td>
            <td className="px-4 py-2.5 align-middle">
              <div className="h-[calc(1.25rem+16px)] flex items-center">
                <button
                  type="button"
                  role="switch"
                  aria-checked={enabled}
                  aria-label={t('config.sensitiveMask.enabled')}
                  disabled={!isConnected}
                  data-testid="config-sensitive-mask-enabled"
                  onClick={() => setEnabled((current) => !current)}
                  className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus:outline-none disabled:cursor-not-allowed disabled:opacity-50 ${enabled ? 'bg-[var(--color-toggle-enabled)]' : 'bg-[var(--color-toggle-disabled)]'}`}
                >
                  <span
                    className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${enabled ? 'translate-x-4' : 'translate-x-0'}`}
                  />
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div className="p-4 space-y-3 border-t border-border">
        {rules.map((rule, index) => (
          <div key={index} className="grid grid-cols-[8rem_1fr_auto] gap-2 items-center">
            <input
              className="rounded-md border border-border bg-bg px-2 py-1.5 text-[13px] text-text-strong outline-none focus:border-accent"
              value={rule.title}
              disabled={!isConnected}
              placeholder={t('config.sensitiveMask.name')}
              data-testid={`config-sensitive-mask-title-${index}`}
              onChange={(event) => updateRule(index, { title: event.target.value })}
            />
            <input
              className="rounded-md border border-border bg-bg px-2 py-1.5 text-[13px] text-text-strong outline-none focus:border-accent"
              value={rule.pattern}
              disabled={!isConnected}
              placeholder={t('config.sensitiveMask.pattern')}
              data-testid={`config-sensitive-mask-pattern-${index}`}
              onChange={(event) => updateRule(index, { pattern: event.target.value })}
            />
            <button
              type="button"
              className="text-danger hover:underline disabled:opacity-50 text-[11px] px-2"
              disabled={!isConnected}
              data-testid={`config-sensitive-mask-remove-${index}`}
              onClick={() => setRules((current) => current.filter((_, itemIndex) => itemIndex !== index))}
            >
              {t('config.sensitiveMask.remove')}
            </button>
          </div>
        ))}
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="btn !px-3 !py-1.5 text-xs disabled:opacity-50"
            disabled={!isConnected}
            data-testid="config-sensitive-mask-add"
            onClick={() => setRules((current) => [...current, { title: '', pattern: '' }])}
          >
            {t('config.sensitiveMask.add')}
          </button>
          {loadError ? <span className="text-xs text-danger">{loadError}</span> : null}
        </div>
      </div>
    </div>
  );
}
