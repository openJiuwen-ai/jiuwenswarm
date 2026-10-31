import { useTranslation } from 'react-i18next';

export interface MacroRoutingPayload {
  mode?: string;
  confidence?: number;
  rationale?: string;
  source?: string;
  gate_confident?: boolean;
}

interface MacroRoutingCardProps {
  routing: MacroRoutingPayload;
}

function modeLabelKey(mode: string | undefined): string {
  switch (mode) {
    case 'team':
      return 'chat.config.mode.cluster';
    case 'agent':
    case 'agent.fast':
    case 'agent.plan':
    default:
      return 'chat.config.mode.singleAgent';
  }
}

export function MacroRoutingCard({ routing }: MacroRoutingCardProps) {
  const { t } = useTranslation();
  const mode = typeof routing.mode === 'string' ? routing.mode : 'agent';
  const confidence =
    typeof routing.confidence === 'number' && Number.isFinite(routing.confidence)
      ? Math.round(routing.confidence * 100)
      : null;

  return (
    <div
      className="w-full max-w-2xl mx-auto my-3 rounded-xl border px-4 py-3 animate-rise"
      style={{
        backgroundColor: 'var(--card)',
        borderColor: 'var(--border)',
      }}
      data-testid="macro-routing-card"
    >
      <div className="flex flex-wrap items-center gap-2 mb-1">
        <span className="text-sm font-semibold" style={{ color: 'var(--text-strong)' }}>
          {t('chatUi.macroRouting.selected', { mode: t(modeLabelKey(mode)) })}
        </span>
        {routing.source && (
          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-[var(--bg-elevated)] border border-[var(--border)] text-[var(--muted)]">
            {t('chatUi.macroRouting.source', { value: routing.source })}
          </span>
        )}
        {confidence != null && (
          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-[var(--bg-elevated)] border border-[var(--border)] text-[var(--muted)]">
            {t('chatUi.macroRouting.confidence', { value: confidence })}
          </span>
        )}
      </div>
      {routing.rationale && (
        <p className="mt-1 text-xs" style={{ color: 'var(--muted)' }}>
          {routing.rationale}
        </p>
      )}
    </div>
  );
}
