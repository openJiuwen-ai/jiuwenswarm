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
      <div className="px-4 py-3 bg-secondary/30 border-b border-border">
        <span className="text-sm font-medium text-text-strong">{t('config.sensitiveMask.title')}</span>
        <span className="block text-xs text-text-muted mt-0.5">{t('config.sensitiveMask.hint')}</span>
      </div>
      <div className="p-4 space-y-3">
        <label className="flex items-center gap-2 text-sm text-text-strong">
          <input
            type="checkbox"
            checked={enabled}
            disabled={!isConnected}
            data-testid="config-sensitive-mask-enabled"
            onChange={(event) => setEnabled(event.target.checked)}
          />
          {t('config.sensitiveMask.enabled')}
        </label>
        {rules.map((rule, index) => (
          <div key={index} className="grid grid-cols-[8rem_1fr_auto] gap-2">
            <input
              className="rounded-md border border-border bg-card px-2 py-1 text-sm text-text-strong"
              value={rule.title}
              disabled={!isConnected}
              placeholder={t('config.sensitiveMask.name')}
              data-testid={`config-sensitive-mask-title-${index}`}
              onChange={(event) => updateRule(index, { title: event.target.value })}
            />
            <input
              className="rounded-md border border-border bg-card px-2 py-1 text-sm text-text-strong"
              value={rule.pattern}
              disabled={!isConnected}
              placeholder={t('config.sensitiveMask.pattern')}
              data-testid={`config-sensitive-mask-pattern-${index}`}
              onChange={(event) => updateRule(index, { pattern: event.target.value })}
            />
            <button
              type="button"
              className="text-sm text-text-muted px-2"
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
            className="text-sm text-text-link"
            disabled={!isConnected}
            data-testid="config-sensitive-mask-add"
            onClick={() => setRules((current) => [...current, { title: '', pattern: '' }])}
          >
            {t('config.sensitiveMask.add')}
          </button>
          {loadError ? <span className="text-xs text-text-muted">{loadError}</span> : null}
        </div>
      </div>
    </div>
  );
}
