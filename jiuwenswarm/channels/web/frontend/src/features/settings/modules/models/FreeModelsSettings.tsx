/**
 * 设置页「模型」tab 顶部的免费模型入口横幅。
 * 活动关闭时不渲染；结束后显示简短说明；进行中显示授权入口。
 */

import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ArrowRight, LogOut } from 'lucide-react';

import { settingsActionIcons } from '../../../../assets/settings';
import freeModelIcon from '../../../../assets/settings/models/free.svg';
import { requestLogin, useAuthStore } from '../../../../stores/authStore';
import { useSessionStore } from '../../../../stores/sessionStore';
import { useFreeModelsCampaign } from '../../../free-models/campaign';
import { FreeModelSettingsDialog } from './FreeModelSettingsDialog';
import { LOGIN_MODEL_SOURCE } from './modelListOperations';
import './FreeModelsSettings.css';

export function FreeModelsSettings() {
  const { t } = useTranslation();
  const campaign = useFreeModelsCampaign();
  const campaignActive = campaign.state === 'active';
  const islogin = useAuthStore((state) => state.islogin);
  const userName = useAuthStore((state) => state.userName);
  const userId = useAuthStore((state) => state.userId);
  const quota = useAuthStore((state) => state.quota);
  const quotaAvailable = useAuthStore((state) => state.quotaAvailable);
  const quotaLoading = useAuthStore((state) => state.quotaLoading);
  const refreshQuota = useAuthStore((state) => state.refreshQuota);
  const availableModels = useSessionStore((state) => state.availableModels);
  const loginModels = useMemo(
    () => availableModels.filter((model) => model.source === LOGIN_MODEL_SOURCE),
    [availableModels],
  );
  const [configOpen, setConfigOpen] = useState(false);

  useEffect(() => {
    if (campaignActive && islogin) void refreshQuota();
  }, [campaignActive, islogin, refreshQuota]);

  if (campaign.state === 'off') return null;

  if (!campaignActive) {
    return (
      <section className="free-models-banner free-models-banner--ended" data-testid="settings-free-models-notice">
        <div className="free-models-banner__copy">
          <strong data-testid="settings-free-models-banner-title">{t('settingsPanel.freeModels.endedTitle')}</strong>
          <p data-testid="settings-free-models-banner-description">{t('settingsPanel.freeModels.endedDescription')}</p>
        </div>
      </section>
    );
  }

  if (islogin) {
    const remainingRatio = quota && quota.total > 0 ? Math.min(1, Math.max(0, quota.balance / quota.total)) : null;
    const percentage = remainingRatio === null ? null : remainingRatio * 100;
    const percentageLabel = percentage === null
      ? '—'
      : `${percentage.toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;

    return (
      <>
        <article className="free-models-card" data-testid="settings-free-models-banner" data-state="signed-in">
          <img className="free-models-card__logo" src={freeModelIcon} alt="" aria-hidden />
          <div className="free-models-card__copy">
            <div className="free-models-card__title-row">
              <strong data-testid="settings-free-models-banner-title">{t('settingsPanel.freeModels.title')}</strong>
              <span className="free-models-card__status">{t('settingsPanel.freeModels.enabled')}</span>
            </div>
            <p data-testid="settings-free-models-banner-description">
              {t('settingsPanel.freeModels.weeklyQuotaDescription')}
            </p>
          </div>
          <div className="free-models-card__actions">
            <div className="free-models-card__quota" data-loading={quotaLoading || undefined}>
              <span>{t('settingsPanel.freeModels.quotaLabel')}</span>
              <span
                className="free-models-card__quota-track"
                role="progressbar"
                aria-label={t('settingsPanel.freeModels.quotaLabel')}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={percentage ?? undefined}
              >
                <span
                  className="free-models-card__quota-value"
                  style={{ width: `${quotaAvailable && percentage !== null ? percentage : 0}%` }}
                />
              </span>
              <span className="free-models-card__quota-percentage">{percentageLabel}</span>
            </div>
            <span className="free-models-card__divider" aria-hidden />
            <button
              type="button"
              className="free-models-card__icon-btn"
              onClick={() => setConfigOpen(true)}
              disabled={loginModels.length === 0}
              aria-label={t('settingsPanel.freeModels.configTitle')}
              title={t('settingsPanel.freeModels.configTitle')}
              data-testid="settings-free-models-config"
            >
              <settingsActionIcons.edit aria-hidden />
            </button>
            <button
              type="button"
              className="free-models-card__icon-btn"
              onClick={() => requestLogin('account')}
              aria-label={t('auth.huawei.logout')}
              title={t('auth.huawei.logout')}
              data-testid="settings-free-models-account"
              data-state="signed-in"
            >
              <LogOut aria-hidden />
            </button>
          </div>
        </article>
        <FreeModelSettingsDialog open={configOpen} models={loginModels} onClose={() => setConfigOpen(false)} />
      </>
    );
  }

  const accountName = userName || userId || '';
  const actionLabel = islogin
    ? accountName || t('settingsPanel.freeModels.signedIn')
    : t('settingsPanel.freeModels.goAuthorize');
  const description = islogin
    ? t('settingsPanel.freeModels.loggedInDescription')
    : t('settingsPanel.freeModels.bannerDescription');

  return (
    <>
      <section className="free-models-banner" data-testid="settings-free-models-banner" data-state={islogin ? 'signed-in' : 'signed-out'}>
        <div className="free-models-banner__copy">
          <strong data-testid="settings-free-models-banner-title">{t('settingsPanel.freeModels.title')}</strong>
          <p data-testid="settings-free-models-banner-description">{description}</p>
        </div>
        <div className="free-models-banner__actions">
          {islogin && loginModels.length > 0 ? (
            <button
              type="button"
              className="free-models-banner__icon-btn"
              onClick={() => setConfigOpen(true)}
              aria-label={t('settingsPanel.freeModels.configTitle')}
              title={t('settingsPanel.freeModels.configTitle')}
              data-testid="settings-free-models-config"
            >
              <settingsActionIcons.edit aria-hidden />
            </button>
          ) : null}
          <button
            type="button"
            className="free-models-banner__action"
            onClick={() => requestLogin()}
            data-testid="settings-free-models-account"
            data-state={islogin ? 'signed-in' : 'signed-out'}
          >
            <span>{actionLabel}</span>
            <ArrowRight size={16} aria-hidden />
          </button>
        </div>
      </section>
      <FreeModelSettingsDialog open={configOpen} models={loginModels} onClose={() => setConfigOpen(false)} />
    </>
  );
}
