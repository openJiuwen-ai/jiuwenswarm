import { Eye, EyeOff } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import feishuIcon from '../../../assets/jiuwenmate/icons/feishu.svg';
import successIcon from '../../../assets/jiuwenmate/icons/success.svg';
import { JiuwenMateDialog } from './JiuwenMateDialog';

interface ImAuthorizeDialogProps {
  onSkip: () => void;
  onConfirm: (credentials: { appId: string; appSecret: string }) => Promise<void>;
  /** True only while introducing a channel for a newly created employee. */
  created?: boolean;
  /** The app id already bound to this employee, when one exists. */
  initialAppId?: string;
}

export function ImAuthorizeDialog({ onSkip, onConfirm, created = true, initialAppId = '' }: ImAuthorizeDialogProps) {
  const { t } = useTranslation();
  const [appId, setAppId] = useState(initialAppId);
  const [secret, setSecret] = useState('');
  const [secretVisible, setSecretVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const canConfirm = appId.trim().length > 0 && secret.trim().length > 0;

  useEffect(() => {
    if (!initialAppId) return;
    setAppId((current) => current || initialAppId);
  }, [initialAppId]);

  const confirm = async () => {
    if (!canConfirm || saving) return;
    setSaving(true);
    setError('');
    try {
      await onConfirm({ appId: appId.trim(), appSecret: secret });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  };

  return (
    <JiuwenMateDialog
      width={600}
      onClose={onSkip}
      testId="jiuwenmate-im-authorize-dialog"
      footer={
        <>
          <button type="button" className="jiuwenmate-outline-button" onClick={onSkip} disabled={saving}>
            {created ? t('jiuwenmate.common.skip') : t('common.cancel')}
          </button>
          <button type="button" className="jiuwenmate-dark-button" disabled={!canConfirm || saving} onClick={() => void confirm()}>
            {t('jiuwenmate.common.confirm')}
          </button>
        </>
      }
    >
      {created ? (
        <div className="jiuwenmate-im-dialog__hero">
          <img src={successIcon} alt="" aria-hidden />
          <strong>{t('jiuwenmate.im.success')}</strong>
        </div>
      ) : null}
      <div className="jiuwenmate-im-dialog__panel">
        <strong>{t('jiuwenmate.im.title')}</strong>
        <small>{created ? t('jiuwenmate.im.hint') : t('jiuwenmate.im.updateHint')}</small>
        <span className="jiuwenmate-im-dialog__platform">
          <img src={feishuIcon} alt="" aria-hidden />
          {t('jiuwenmate.im.feishu')}
        </span>
        <label className="jiuwenmate-field">
          <span>{t('jiuwenmate.im.appId')}</span>
          <input value={appId} onChange={(event) => setAppId(event.target.value)} placeholder={t('jiuwenmate.im.appIdPlaceholder')} autoComplete="off" />
        </label>
        <label className="jiuwenmate-field">
          <span>{t('jiuwenmate.im.secret')}</span>
          <span className="jiuwenmate-field__secret">
            <input
              type={secretVisible ? 'text' : 'password'}
              value={secret}
              onChange={(event) => setSecret(event.target.value)}
              placeholder={t('jiuwenmate.im.secretPlaceholder')}
              autoComplete="new-password"
            />
            <button
              type="button"
              aria-label={secretVisible ? t('jiuwenmate.im.hideSecret') : t('jiuwenmate.im.showSecret')}
              onClick={() => setSecretVisible((current) => !current)}
            >
              {secretVisible ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
            </button>
          </span>
        </label>
        {error ? <p className="jiuwenmate-action-error" role="alert">{error}</p> : null}
      </div>
    </JiuwenMateDialog>
  );
}
