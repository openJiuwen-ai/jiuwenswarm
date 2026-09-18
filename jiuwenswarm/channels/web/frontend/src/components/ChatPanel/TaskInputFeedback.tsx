import { useTranslation } from 'react-i18next';
import type { TaskInputReceipt } from '../../stores/chatStore';

/** UI-only receipts beside execution metadata; never part of model reasoning. */
export function TaskInputFeedback({ receipts }: { receipts: TaskInputReceipt[] }) {
  const { t } = useTranslation();
  if (!receipts.length) return null;

  return (
    <span
      className="ml-2 inline-flex min-w-0 max-w-[50vw] gap-2 text-xs font-normal text-text-meta"
      role="status"
      aria-label={t('network.supplementResults')}
      data-testid="chat-panel-task-input-feedback"
    >
      {receipts.map((receipt) => {
        const status = {
          sending: t('network.supplementSending'),
          accepted: t('network.supplementAccepted'),
          failed: t('network.supplementDeliveryFailed', { message: receipt.error || t('network.requestFailed') }),
          unknown: t('network.supplementDeliveryUnknown', { message: receipt.error || t('network.requestTimeout') }),
        }[receipt.status];
        const detail = `${receipt.content}\n${status}`;
        return (
          <span
            key={receipt.taskId}
            className="min-w-0 truncate"
            title={detail}
            aria-label={detail}
            data-testid="chat-panel-task-input-receipt"
            data-variant={receipt.taskId}
          >
            <span data-testid="chat-panel-task-input-status" data-variant={receipt.status}>
              {status}
            </span>
          </span>
        );
      })}
    </span>
  );
}
