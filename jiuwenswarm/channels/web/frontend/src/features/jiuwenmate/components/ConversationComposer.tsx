import { ChevronDown, Plus, Send, ShieldUser, Square } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { type JiuwenMateModelCatalog } from '../jiuwenmateApi';
import { ModelLabel } from './ModelLabel';

interface ConversationComposerProps {
  value: string;
  onChange: (value: string) => void;
  /** Without it the composer only edits text and cannot send. */
  onSend?: () => void;
  sending?: boolean;
  /** Given while a reply is running: the send button becomes a stop button. */
  onStop?: () => void;
  /** The stop was requested and the reply has not ended yet. */
  stopping?: boolean;
  /** Models available on this server, used to name the colleague's configured model. */
  modelCatalog: JiuwenMateModelCatalog;
  /** The model saved on the colleague. Empty means the server default. */
  modelId: string;
}

export function ConversationComposer({
  value,
  onChange,
  onSend,
  sending = false,
  onStop,
  stopping = false,
  modelCatalog,
  modelId,
}: ConversationComposerProps) {
  const { t } = useTranslation();
  const canSend = Boolean(onSend) && !sending && value.trim().length > 0;
  return (
    <div className="jiuwenmate-composer" data-testid="jiuwenmate-conversation-composer">
      <textarea
        rows={2}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
          event.preventDefault();
          if (canSend) onSend?.();
        }}
        placeholder={t('jiuwenmate.workspace.composerPlaceholder')}
        data-testid="jiuwenmate-composer-input"
      />
      <div className="jiuwenmate-composer__toolbar">
        <span className="jiuwenmate-composer__left">
          <button type="button" aria-label={t('jiuwenmate.workspace.attach')}>
            <Plus aria-hidden />
          </button>
          <button type="button" className="jiuwenmate-composer__mode">
            <ShieldUser aria-hidden />
            <span>{t('jiuwenmate.workspace.defaultPermission')}</span>
            <ChevronDown aria-hidden />
          </button>
        </span>
        <span className="jiuwenmate-composer__right">
          <span className="jiuwenmate-composer__model" data-testid="jiuwenmate-composer-model">
            <ModelLabel catalog={modelCatalog} modelId={modelId} />
          </span>
          {onStop ? (
            <button
              type="button"
              className="jiuwenmate-composer__send jiuwenmate-composer__send--stop"
              aria-label={t('jiuwenmate.workspace.stop')}
              title={t('jiuwenmate.workspace.stop')}
              disabled={stopping}
              onClick={onStop}
              data-testid="jiuwenmate-composer-stop"
            >
              <Square aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              className="jiuwenmate-composer__send"
              aria-label={t('jiuwenmate.workspace.send')}
              disabled={!canSend}
              onClick={onSend}
              data-testid="jiuwenmate-composer-send"
            >
              <Send aria-hidden />
            </button>
          )}
        </span>
      </div>
    </div>
  );
}
