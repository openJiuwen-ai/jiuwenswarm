import { Eye, EyeOff } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import feishuIcon from '../../../assets/jiuwenmate/icons/feishu.svg';
import dingtalkIcon from '../../../assets/settings/channels/dingtalk.svg';
import successIcon from '../../../assets/jiuwenmate/icons/success.svg';
import { JiuwenMateDialog } from './JiuwenMateDialog';

type ImPlatform = 'feishu' | 'dingtalk';

interface ImAuthorizeDialogProps {
  onSkip: () => void;
  onConfirm: (credentials: { platform: ImPlatform; appId: string; appSecret: string; robotCode?: string }) => Promise<void>;
  created?: boolean;
  initialAppId?: string;
  initialPlatform?: ImPlatform;
  initialRobotCode?: string;
}

export function ImAuthorizeDialog({
  onSkip, onConfirm, created = true, initialAppId = '', initialPlatform = 'feishu', initialRobotCode = '',
}: ImAuthorizeDialogProps) {
  const { t } = useTranslation();
  const [platform, setPlatform] = useState<ImPlatform>(initialPlatform);
  const [appId, setAppId] = useState(initialAppId);
  const [robotCode, setRobotCode] = useState(initialRobotCode);
  const [secret, setSecret] = useState('');
  const [secretVisible, setSecretVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const canConfirm = appId.trim().length > 0 && secret.trim().length > 0;

  useEffect(() => { setPlatform(initialPlatform); }, [initialPlatform]);
  useEffect(() => {
    if (initialAppId) setAppId((current) => current || initialAppId);
  }, [initialAppId]);
  useEffect(() => {
    if (initialRobotCode) setRobotCode((current) => current || initialRobotCode);
  }, [initialRobotCode]);

  const confirm = async () => {
    if (!canConfirm || saving) return;
    setSaving(true);
    setError('');
    try {
      await onConfirm({ platform, appId: appId.trim(), appSecret: secret, robotCode: robotCode.trim() });
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
          <button data-testid="jiuwenmate-im-cancel" type="button" className="jiuwenmate-outline-button" onClick={onSkip} disabled={saving}>
            {created ? t('jiuwenmate.common.skip') : t('common.cancel')}
          </button>
          <button data-testid="jiuwenmate-im-confirm" type="button" className="jiuwenmate-dark-button" disabled={!canConfirm || saving} onClick={() => void confirm()}>
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
        <small>{platform === 'dingtalk' ? t('jiuwenmate.im.dingtalkHint') : created ? t('jiuwenmate.im.hint') : t('jiuwenmate.im.updateHint')}</small>
        <label className="jiuwenmate-field">
          <span>{t('jiuwenmate.im.platform')}</span>
          <select
            data-testid="jiuwenmate-im-platform-select"
            value={platform}
            disabled={!created || saving}
            onChange={(event) => {
              setPlatform(event.target.value as ImPlatform);
              setAppId(''); setSecret(''); setRobotCode(''); setError('');
            }}
          >
            <option value="feishu">{t('jiuwenmate.im.feishu')}</option>
            <option value="dingtalk">{t('jiuwenmate.im.dingtalk')}</option>
          </select>
        </label>
        <span className="jiuwenmate-im-dialog__platform">
          <img src={platform === 'dingtalk' ? dingtalkIcon : feishuIcon} alt="" aria-hidden />
          {t(`jiuwenmate.im.${platform}`)}
        </span>
        <small data-testid="jiuwenmate-im-isolation-hint">{t('jiuwenmate.im.isolationHint')}</small>
        <label className="jiuwenmate-field">
          <span>{platform === 'dingtalk' ? t('jiuwenmate.im.clientId') : t('jiuwenmate.im.appId')}</span>
          <input data-testid="jiuwenmate-im-app-id" value={appId} onChange={(event) => setAppId(event.target.value)} placeholder={t('jiuwenmate.im.appIdPlaceholder')} autoComplete="off" />
        </label>
        <label className="jiuwenmate-field">
          <span>{t('jiuwenmate.im.secret')}</span>
          <span className="jiuwenmate-field__secret">
            <input data-testid="jiuwenmate-im-secret" type={secretVisible ? 'text' : 'password'} value={secret} onChange={(event) => setSecret(event.target.value)} placeholder={t('jiuwenmate.im.secretPlaceholder')} autoComplete="new-password" />
            <button data-testid="jiuwenmate-im-toggle-secret" type="button" aria-label={secretVisible ? t('jiuwenmate.im.hideSecret') : t('jiuwenmate.im.showSecret')} onClick={() => setSecretVisible((current) => !current)}>
              {secretVisible ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
            </button>
          </span>
        </label>
        {platform === 'dingtalk' ? (
          <label className="jiuwenmate-field">
            <span>{t('jiuwenmate.im.robotCode')}</span>
            <input data-testid="jiuwenmate-im-robot-code" value={robotCode} onChange={(event) => setRobotCode(event.target.value)} placeholder={t('jiuwenmate.im.robotCodePlaceholder')} autoComplete="off" />
          </label>
        ) : null}
        {error ? <p data-testid="jiuwenmate-im-error" className="jiuwenmate-action-error" role="alert">{error}</p> : null}
      </div>
    </JiuwenMateDialog>
  );
}
