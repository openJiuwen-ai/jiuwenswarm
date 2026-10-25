import { ChevronDown, Plus, Send, ShieldUser, Square } from 'lucide-react';
import { useState } from 'react';
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
  /** The permission profile saved on the colleague. */
  permissionProfile: 'default' | 'automatic' | 'full_access';
  /** Saves and publishes a new colleague permission profile. */
  onPermissionProfileChange?: (profile: 'default' | 'automatic' | 'full_access') => void;
  permissionProfileSaving?: boolean;
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
  permissionProfile,
  onPermissionProfileChange,
  permissionProfileSaving = false,
}: ConversationComposerProps) {
  const { t } = useTranslation();
  const [permissionMenuOpen, setPermissionMenuOpen] = useState(false);
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
        <div className="jiuwenmate-composer__left">
          <button type="button" aria-label={t('jiuwenmate.workspace.attach')}>
            <Plus aria-hidden />
          </button>
          <div
            className="jiuwenmate-composer__permission"
            onKeyDown={(event) => {
              if (event.key === 'Escape') setPermissionMenuOpen(false);
            }}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setPermissionMenuOpen(false);
            }}
          >
            <button
              type="button"
              className="jiuwenmate-composer__mode"
              aria-expanded={permissionMenuOpen}
              aria-haspopup="menu"
              disabled={!onPermissionProfileChange || permissionProfileSaving}
              onClick={() => setPermissionMenuOpen((open) => !open)}
              data-testid="jiuwenmate-composer-permission-trigger"
              data-variant={permissionProfile}
            >
              <ShieldUser aria-hidden />
              <span>{t(`jiuwenmate.profile.permissions.profiles.${permissionProfile}`)}</span>
              <ChevronDown aria-hidden />
            </button>
            {permissionMenuOpen ? (
              <div
                className="jiuwenmate-composer__permission-menu"
                role="menu"
                aria-label={t('jiuwenmate.profile.permissions.title')}
                data-testid="jiuwenmate-composer-permission-menu"
              >
                {(['default', 'automatic', 'full_access'] as const).map((profile) => (
                  <button
                    key={profile}
                    type="button"
                    role="menuitemradio"
                    aria-checked={permissionProfile === profile}
                    disabled={permissionProfileSaving}
                    className="jiuwenmate-composer__permission-option"
                    onClick={() => {
                      setPermissionMenuOpen(false);
                      onPermissionProfileChange?.(profile);
                    }}
                    data-testid="jiuwenmate-composer-permission-option"
                    data-variant={profile}
                  >
                    <strong>{t(`jiuwenmate.profile.permissions.profiles.${profile}`)}</strong>
                    <small>{t(`jiuwenmate.profile.permissions.profileHints.${profile}`)}</small>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
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
