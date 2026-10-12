import { Eye, EyeOff } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import feishuIcon from '../../../assets/jiuwenmate/icons/feishu.svg';
import successIcon from '../../../assets/jiuwenmate/icons/success.svg';
import { JiuwenMateDialog } from './JiuwenMateDialog';

interface ImAuthorizeDialogProps {
  onSkip: () => void;
  onConfirm: () => void;
}

export function ImAuthorizeDialog({ onSkip, onConfirm }: ImAuthorizeDialogProps) {
  const { t } = useTranslation();
  const [appId, setAppId] = useState('');
  const [secret, setSecret] = useState('');
  const [secretVisible, setSecretVisible] = useState(false);
  const canConfirm = appId.trim().length > 0 && secret.trim().length > 0;

  return (
    <JiuwenMateDialog
      width={600}
      onClose={onSkip}
      testId="jiuwenmate-im-authorize-dialog"
      footer={
        <>
          <button type="button" className="jiuwenmate-outline-button" onClick={onSkip}>
            {t('jiuwenmate.common.skip')}
          </button>
          <button type="button" className="jiuwenmate-dark-button" disabled={!canConfirm} onClick={onConfirm}>
            {t('jiuwenmate.common.confirm')}
          </button>
        </>
      }
    >
      <div className="jiuwenmate-im-dialog__hero">
        <img src={successIcon} alt="" aria-hidden />
        <strong>{t('jiuwenmate.im.success')}</strong>
      </div>
      <div className="jiuwenmate-im-dialog__panel">
        <strong>{t('jiuwenmate.im.title')}</strong>
        <small>{t('jiuwenmate.im.hint')}</small>
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
      </div>
    </JiuwenMateDialog>
  );
}
