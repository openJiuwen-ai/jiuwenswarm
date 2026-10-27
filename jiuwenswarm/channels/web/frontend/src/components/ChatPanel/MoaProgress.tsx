import { useTranslation } from 'react-i18next';
import type { MoaOperation, MoaPhase } from '../../types';

export function activeMoaPhase(operations: Record<string, MoaOperation> | undefined): MoaPhase | undefined {
  const active = Object.values(operations ?? {}).filter((operation) => operation.status === 'started');
  if (active.some((operation) => operation.phase === 'verification')) return 'verification';
  return active.length > 0 ? 'reference' : undefined;
}

export function MoaProgress({ phase }: { phase?: MoaPhase }) {
  const { t } = useTranslation();
  if (!phase) return null;
  return (
    <div
      className="mt-2 flex items-center gap-1.5 text-xs text-text-muted"
      role="status"
      aria-live="polite"
      data-testid="chat-panel-moa-progress"
      data-variant={phase}
    >
      <span className="context-compression-running-text" data-testid="chat-panel-moa-progress-label">
        {t(phase === 'verification' ? 'chat.moaVerifying' : 'chat.moaReferenceRunning')}
      </span>
    </div>
  );
}
