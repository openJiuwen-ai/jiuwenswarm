/**
 * 设置页「模型」下的「限时免费模型」：一个卡片两行——账号（登录入口）和免费积分，
 * 账号决定有没有积分，所以同框用分隔线隔开。
 *
 * 活动没在跑时：1、配置显示设置结束，2、拉不到配置时：两种情况文案
 * 一样显示活动结束，本地显式关掉（`off`）才整块不渲染。
 * 配置恢复后（窗口重新获得焦点时会重查）入口自己回来。
 * **标题也在这里自己渲染**，不交给模块定义的 `titleKey`，否则不渲染时会剩一个空标题。
 */

import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';

import { ChevronRight, LogIn, RefreshCw } from 'lucide-react';

import { Tag } from '../../../../components/ui';
import type { ModelQuota } from '../../../../services/authClient';
import { requestLogin, useAuthStore } from '../../../../stores/authStore';
import { useFreeModelsCampaign } from '../../../free-models/campaign';
import { formatPoints, usedRatio } from '../../../free-models/points';
import { describeQuotaExhausted, describeQuotaReset } from '../../../free-models/quotaReset';
import { SettingRow, SettingsSection } from '../../components';
import './FreeModelsSettings.css';

interface QuotaView {
  islogin: boolean;
  quota: ModelQuota | null;
  loading: boolean;
  locale: string;
}

/** 积分行右侧：剩余积分，或者一句状态。 */
function PointsHeadline({ islogin, quota, loading, locale }: QuotaView) {
  const { t } = useTranslation();
  const status = (text: string, tone: 'muted' | 'danger' = 'muted') => (
    <span className="free-models-quota__status" data-tone={tone} data-testid="settings-free-models-quota">
      {text}
    </span>
  );

  if (!islogin) return status(t('settingsPanel.freeModels.quotaLoggedOut'));
  // 刷新时手上已有数就继续显示，只有第一次查才显示"查询中"，免得每次进页面数字闪一下
  if (quota === null) return status(loading ? t('auth.huawei.quota.loading') : t('auth.huawei.quota.unknown'));
  if (quota.exhausted) return status(t('auth.huawei.quota.exhausted'), 'danger');
  if (!(quota.balance >= 0)) return status(t('auth.huawei.quota.unknown'));

  const value = formatPoints(quota.balance, locale);
  const unit = t('auth.huawei.quota.unit');
  return (
    <span
      className="free-models-points"
      data-tone={quota.low_balance ? 'warning' : 'normal'}
      data-testid="settings-free-models-quota"
      aria-label={`${t('auth.huawei.quota.remaining')} ${value} ${unit}`}
    >
      <span className="free-models-points__label" aria-hidden>
        {t('auth.huawei.quota.remaining')}
      </span>
      <span className="free-models-points__value" aria-hidden>
        {value}
      </span>
      <span className="free-models-points__unit" aria-hidden>
        {unit}
      </span>
    </span>
  );
}

/**
 * 积分行的说明，回答"积分会不会回来、什么时候回来"：用完了说何时恢复；平时有周期就说周期
 * （每周一 08:00 刷新），没有周期就说清楚按用量扣减、用完即止。
 */
function PointsHint({ islogin, quota, locale }: QuotaView) {
  const { t } = useTranslation();
  if (!islogin || quota === null) return <>{t('auth.huawei.quota.meteringHint')}</>;
  const reset = quota.exhausted
    ? describeQuotaExhausted(quota.reset_at, locale)
    : describeQuotaReset(quota.reset_period, quota.reset_at, locale);
  if (!reset) {
    return <>{quota.exhausted ? t('auth.huawei.quota.exhaustedHint') : t('auth.huawei.quota.meteringHint')}</>;
  }
  return (
    <span className="free-models-quota__hint">
      <RefreshCw size={13} aria-hidden />
      {t(reset.key, reset.params)}
    </span>
  );
}

/** 有没有可画的用量明细：进度条或已用数至少知道一个。没有时整个明细区（连同分隔线）都不出现。 */
function hasUsage({ islogin, quota }: QuotaView): boolean {
  return islogin && quota !== null && (usedRatio(quota) !== null || quota.used >= 0);
}

/** 进度条（已用占比）和两端的已用 / 总量。调用方先用 {@link hasUsage} 判断。 */
function PointsUsage({ quota, locale }: { quota: ModelQuota; locale: string }) {
  const { t } = useTranslation();
  const ratio = usedRatio(quota);
  const showUsed = quota.used >= 0;
  const showTotal = quota.total >= 0;

  const tone = quota.exhausted ? 'danger' : quota.low_balance ? 'warning' : 'normal';
  return (
    <div className="free-models-quota__usage" data-testid="settings-free-models-quota-detail">
      {/* 进度条只在总量和已用都知道时画：画一条没有依据的条比不画更误导 */}
      {ratio !== null && (
        <div
          className="free-models-quota__meter"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(ratio * 100)}
          aria-label={t('auth.huawei.quota.used')}
        >
          <span
            className="free-models-quota__meter-fill"
            data-tone={tone}
            // 刚用了一点也露出一截，让人看得出已经开始扣了
            style={{ width: `${ratio > 0 ? Math.max(2, ratio * 100) : 0}%` }}
          />
        </div>
      )}
      <div className="free-models-quota__legend">
        {showUsed && (
          <span>
            {t('auth.huawei.quota.used')}
            <strong>{formatPoints(quota.used, locale)}</strong>
          </span>
        )}
        {showTotal && (
          <span>
            {t('auth.huawei.quota.total')}
            <strong>{formatPoints(quota.total, locale)}</strong>
          </span>
        )}
      </div>
    </div>
  );
}

export function FreeModelsSettings() {
  const { t, i18n } = useTranslation();
  const campaign = useFreeModelsCampaign();
  const campaignActive = campaign.state === 'active';
  const islogin = useAuthStore((state) => state.islogin);
  const userName = useAuthStore((state) => state.userName);
  const userId = useAuthStore((state) => state.userId);
  const quota = useAuthStore((state) => state.quota);
  const quotaAvailable = useAuthStore((state) => state.quotaAvailable);
  const quotaLoading = useAuthStore((state) => state.quotaLoading);
  const refreshQuota = useAuthStore((state) => state.refreshQuota);

  // 进设置页时查一次，登录态变了再查一次。积分会被对话消耗，缓着显示不如现查。
  useEffect(() => {
    if (campaignActive && islogin) void refreshQuota();
  }, [campaignActive, islogin, refreshQuota]);

  if (campaign.state === 'off') return null;
  if (!campaignActive) {
    return (
      <SettingsSection title={t('settingsPanel.freeModels.title')}>
        <div className="settings-page__item">
          <SettingRow
            title={t('settingsPanel.freeModels.endedTitle')}
            description={t('settingsPanel.freeModels.endedDescription')}
            data-testid="settings-free-models-notice"
          />
        </div>
      </SettingsSection>
    );
  }

  const accountName = userName || userId || '';
  // 这套部署没接额度服务时整行不出现：显示"用量未知"只会让人以为出了问题。
  const showQuotaRow = !islogin || quotaAvailable;
  const view: QuotaView = { islogin, quota, loading: quotaLoading, locale: i18n.language || 'zh-CN' };
  const lowBalance = islogin && quota !== null && quota.low_balance && !quota.exhausted;

  return (
    <SettingsSection title={t('settingsPanel.freeModels.title')}>
      {/* 两行装在同一个 settings-page__item 里，卡片的分隔线才会落在它们之间
          （见 SettingsSection.css 里 `.settings-page__item > .setting-row + .setting-row`）。 */}
      <div className="settings-page__item">
        {/* 整行可点：点哪儿都是打开账号面板（登录 / 查看账号 / 退出）。
            与其在右边再放一个按钮，不如让这一行本身就是那个按钮——右边留一个
            图标说明"点了会打开东西"。 */}
        <div className="setting-row">
          <button
            type="button"
            className="setting-row__main free-models-account"
            onClick={() => requestLogin()}
            data-testid="settings-free-models-account"
            data-state={islogin ? 'signed-in' : 'signed-out'}
          >
            <span className="setting-row__copy">
              <span className="setting-row__title-line">
                <span className="setting-row__title">
                  {islogin ? accountName : t('settingsPanel.freeModels.loginTitle')}
                </span>
                {islogin && (
                  <Tag variant="success" data-testid="settings-free-models-signed-in">
                    {t('settingsPanel.freeModels.signedIn')}
                  </Tag>
                )}
              </span>
              <span className="setting-row__description">
                {islogin
                  ? t('settingsPanel.freeModels.loggedInDescription')
                  : t('settingsPanel.freeModels.loginDescription')}
              </span>
            </span>
            <span className="setting-row__control">
              {islogin ? (
                <ChevronRight className="free-models-account__chevron" size={18} aria-hidden />
              ) : (
                <span className="free-models-account__badge" aria-hidden>
                  <LogIn size={16} />
                </span>
              )}
            </span>
          </button>
        </div>

        {showQuotaRow && (
          <SettingRow
            className="free-models-quota"
            title={t('settingsPanel.freeModels.quotaTitle')}
            meta={lowBalance ? <Tag variant="warning">{t('auth.huawei.quota.lowBalanceHint')}</Tag> : null}
            description={<PointsHint {...view} />}
            controlPlacement="top"
            subSettings={
              quota !== null && hasUsage(view) ? <PointsUsage quota={quota} locale={view.locale} /> : null
            }
          >
            <PointsHeadline {...view} />
          </SettingRow>
        )}
      </div>
    </SettingsSection>
  );
}
