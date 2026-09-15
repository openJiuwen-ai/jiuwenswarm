/**
 * PersonalContextSettingsPanel — 上下文设置页。
 *
 * 位于左侧导航「更多」抽屉（浏览器之后）。
 * 自取 webClient（与 SkillPanel 一致），仅靠 isConnected 做就绪门控。
 * 数据与写操作走 usePersonalContextStore（乐观更新 + 失败回滚）。
 *
 * 本页职责：启用/自动更新/模式/模型 + 内容采集授权（飞书 OAuth 真接口 + GitHub/GitCode PAT 后端校验落盘）。
 * 采集来源的创建统一在「上下文内容」页的添加内容抽屉完成，本页不再承担创建。
 */

import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, X } from 'lucide-react';
import { Switch } from '../Switch';
import ModelPicker from '../ModelPicker';
import { usePersonalContextStore } from '../../stores';
import { useSessionStore } from '../../stores';
import { STRATEGY_OPTIONS } from '../../services/personalContextApi';
import './SettingsPanel.css';
import feishuLogo from '../../assets/settings/channels/feishu.svg';
import githubLogo from '../../assets/settings/channels/GitHub.svg';
import gitcodeLogo from '../../assets/settings/channels/gitcode.png';
interface PersonalContextSettingsPanelProps {
  isConnected: boolean;
}

export function PersonalContextSettingsPanel({
  isConnected,
}: PersonalContextSettingsPanelProps) {
  const { t } = useTranslation();
  const {
    config,
    status,
    loadingConfig,
    pendingWrites,
    loadAll,
    setMasterEnabled,
    setEnabled,
    setStrategyProfile,
    selectModel,
    loadAuthStatus,
    authorizeProvider,
    authByProvider,
  } = usePersonalContextStore();
  const availableModels = useSessionStore((s) => s.availableModels);

  // 总开关为派生状态：任一子开关开启即视为开启。
  const masterEnabled = config.collection_enabled || config.agent_use_enabled;

  const [error, setError] = useState<string | null>(null);
  const [githubModalOpen, setGithubModalOpen] = useState(false);
  const [gitcodeModalOpen, setGitcodeModalOpen] = useState(false);

  // 后端 stored_config 落盘后不带 configured 字段，只有 PersonalContextStatus 稳定带。
  // 因此"是否已配置"以 status.configured 为准，而非 config.configured。
  const isConfigured = status?.configured === true || config.collection_enabled === true;

  useEffect(() => {
    if (!isConnected) return;
    void loadAll().catch((e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    });
    // 进入设置页时拉一次各授权源状态（飞书 OAuth 态 + github/gitcode PAT 态，即便尚未创建服务也只读）
    for (const provider of ['feishu', 'github', 'gitcode'] as const) {
      void loadAuthStatus(provider).catch(() => {
        // 静默；授权状态读取失败不阻塞主流程
      });
    }
  }, [isConnected, loadAll, loadAuthStatus]);

  const handleEnabled = useCallback(
    (enabled: boolean) => {
      setError(null);
      void setEnabled(enabled).catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [setEnabled],
  );

  const handleMasterEnabled = useCallback(
    (enabled: boolean) => {
      setError(null);
      void setMasterEnabled(enabled).catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [setMasterEnabled],
  );

  const handleStrategy = useCallback(
    (profile: 'rules' | 'balanced' | 'agent') => {
      setError(null);
      // balanced/agent 需要模型；未选时前端拦截
      if (profile !== 'rules' && config.model_index == null) {
        setError(t('personalContext.settings.modelRequiredFirst'));
        return;
      }
      void setStrategyProfile(profile).catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [config.model_index, setStrategyProfile, t],
  );

  const handleModel = useCallback(
    (index: number) => {
      setError(null);
      void selectModel(index).catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [selectModel],
  );

  const handleFeishuAuthorize = useCallback(() => {
    setError(null);
    void authorizeProvider('feishu').then((result) => {
      // 收到 verification_url 后在新窗口打开飞书授权页
      if (result?.verification_url) {
        window.open(result.verification_url, '_blank', 'noopener,noreferrer');
      }
    }).catch((e: unknown) => {
      setError(e instanceof Error ? e.message : String(e));
    });
  }, [authorizeProvider]);

  const feishuAuth = authByProvider.feishu;
  const feishuState = feishuAuth?.state ?? 'not_authorized';
  const githubState = authByProvider.github?.state ?? 'not_authorized';
  const gitcodeState = authByProvider.gitcode?.state ?? 'not_authorized';

  // 飞书授权中（设备流需用户在浏览器完成）时轮询状态，直到变 authorized/failed
  useEffect(() => {
    if (!isConnected || feishuState !== 'authorizing') return;
    const id = window.setInterval(() => void loadAuthStatus('feishu'), 5000);
    return () => window.clearInterval(id);
  }, [isConnected, feishuState, loadAuthStatus]);

  if (loadingConfig && !isConfigured) {
    return (
      <div className="pc-settings pc-settings--loading">
        <Loader2 className="spin" size={20} />
      </div>
    );
  }

  return (
    <div className="pc-settings" data-testid="personal-context-settings">
      {error && (
        <div className="pc-settings__error" role="alert">
          {error}
        </div>
      )}

      {/* 配置卡片：总开关关闭时只显示总开关；开启后显示采集开关与完整配置 */}
      <div className="pc-settings__card">
        {/* 总开关 */}
        <div className="pc-settings__card-row">
          <div className="pc-settings__row-text">
            <div className="pc-settings__row-label">{t('personalContext.settings.masterEnable')}</div>
            <div className="pc-settings__row-hint">{t('personalContext.settings.masterEnableHint')}</div>
          </div>
          <Switch
            checked={masterEnabled}
            onChange={handleMasterEnabled}
            disabled={!isConnected || !!pendingWrites.collection_enabled || !!pendingWrites.agent_use_enabled}
          />
        </div>

        {masterEnabled && (
          <>
            {/* 采集个人上下文内容 */}
            <div className="pc-settings__card-row">
              <div className="pc-settings__row-text">
                <div className="pc-settings__row-label">{t('personalContext.settings.enable')}</div>
                <div className="pc-settings__row-hint">{t('personalContext.settings.enableHint')}</div>
              </div>
              <Switch
                checked={config.collection_enabled}
                onChange={handleEnabled}
                disabled={!isConnected || !!pendingWrites.collection_enabled}
              />
            </div>

            {config.collection_enabled && (
              <>
            {/* 上下文采集模式 */}
            <div className="pc-settings__card-row pc-settings__card-row--inline">
              <div className="pc-settings__row-text">
                <div className="pc-settings__row-label">{t('personalContext.settings.strategyProfile')}</div>
                <div className="pc-settings__row-hint">{t('personalContext.settings.subtitle')}</div>
              </div>
              <select
                className="pc-settings__select"
                value={config.strategy_profile}
                onChange={(e) => handleStrategy(e.target.value as 'rules' | 'balanced' | 'agent')}
                disabled={!isConnected || !!pendingWrites.strategy_profile}
              >
                {STRATEGY_OPTIONS.map((s) => (
                  <option key={s} value={s}>{t('personalContext.settings.strategy_' + s)}</option>
                ))}
              </select>
            </div>

            {/* 上下文整理模型 */}
            <div className="pc-settings__card-row pc-settings__card-row--inline">
              <div className="pc-settings__row-text">
                <div className="pc-settings__row-label">{t('personalContext.settings.model')}</div>
                <div className="pc-settings__row-hint">{t('personalContext.settings.modelHint')}</div>
              </div>
              <ModelPicker
                testIdPrefix="personal-context-model"
                value={config.model_index != null ? availableModels[config.model_index]?.model_name ?? null : null}
                onChange={(modelName) => {
                  const idx = availableModels.findIndex((m) => m.model_name === modelName);
                  if (idx >= 0) handleModel(idx);
                }}
                disabled={!isConnected || !!pendingWrites.model_index || availableModels.length === 0}
              />
            </div>

            {/* 内容采集授权 */}
            <div className="pc-settings__card-row pc-settings__card-row--auth">
              <div className="pc-settings__auth-head">
                <div className="pc-settings__row-text">
                  <div className="pc-settings__row-label">{t('personalContext.authorization.title')}</div>
                  <div className="pc-settings__row-hint">{t('personalContext.authorization.subtitle')}</div>
                </div>
              </div>
              <div className="pc-settings__auth-cards">
                {/* 飞书 */}
                <div className="pc-settings__auth-card">
                  <div className="pc-settings__auth-icon pc-settings__auth-icon--feishu"><img src={feishuLogo} alt="飞书" /></div>
                  <span className="pc-settings__auth-name">{t('personalContext.provider.feishu')}</span>
                  <button
                    type="button"
                    className="pc-settings__auth-action"
                    onClick={handleFeishuAuthorize}
                    disabled={!isConnected || !!pendingWrites['auth:feishu'] || feishuState === 'authorizing'}
                  >
                    {feishuState === 'authorizing'
                      ? t('personalContext.authorization.authorizing')
                      : feishuState === 'authorized'
                        ? t('personalContext.authorization.reauthorize')
                        : t('personalContext.authorization.authorize')}
                  </button>
                </div>
                {/* GitHub */}
                <div className="pc-settings__auth-card">
                  <div className="pc-settings__auth-icon pc-settings__auth-icon--github"><img src={githubLogo} alt="GitHub" /></div>
                  <span className="pc-settings__auth-name">{t('personalContext.provider.github')}</span>
                  <button
                    type="button"
                    className="pc-settings__auth-action"
                    onClick={() => setGithubModalOpen(true)}
                    disabled={!isConnected}
                  >
                    {githubState === 'authorized'
                      ? t('personalContext.authorization.reauthorize')
                      : t('personalContext.authorization.authorize')}
                  </button>
                </div>
                {/* GitCode */}
                <div className="pc-settings__auth-card">
                  <div className="pc-settings__auth-icon pc-settings__auth-icon--gitcode"><img src={gitcodeLogo} alt="GitCode" /></div>
                  <span className="pc-settings__auth-name">{t('personalContext.provider.gitcode')}</span>
                  <button
                    type="button"
                    className="pc-settings__auth-action"
                    onClick={() => setGitcodeModalOpen(true)}
                    disabled={!isConnected}
                  >
                    {gitcodeState === 'authorized'
                      ? t('personalContext.authorization.reauthorize')
                      : t('personalContext.authorization.authorize')}
                  </button>
                </div>
              </div>
            </div>
          </>
        )}
          </>
        )}
      </div>

      {githubModalOpen && (
        <GithubTokenModal
          onClose={() => setGithubModalOpen(false)}
          onSave={async (token) => {
            await authorizeProvider('github', { token });
          }}
        />
      )}

      {gitcodeModalOpen && (
        <GitcodeTokenModal
          onClose={() => setGitcodeModalOpen(false)}
          onSave={async (pat) => {
            await authorizeProvider('gitcode', { pat });
          }}
        />
      )}
    </div>
  );
}

/**
 * GitHub PAT 输入弹窗：提交后走后端 authorize_provider 真实校验并落盘（不再用 localStorage mock）。
 */
function GithubTokenModal({
  onClose,
  onSave,
}: {
  onClose: () => void;
  onSave: (token: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    const trimmed = token.trim();
    if (!trimmed) {
      setError(t('personalContext.authorization.githubTokenRequired'));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave(trimmed);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="pc-settings__modal-overlay" onClick={onClose}>
      <div className="pc-settings__modal" onClick={(e) => e.stopPropagation()}>
        <div className="pc-settings__modal-head">
          <h3 className="pc-settings__modal-title">{t('personalContext.authorization.authorize')} · {t('personalContext.provider.github')}</h3>
          <button type="button" className="pc-settings__modal-close" onClick={onClose} aria-label="close">
            <X size={16} />
          </button>
        </div>
        <div className="pc-settings__field">
          <label>{t('personalContext.authorization.githubTokenLabel')}</label>
          <input
            className="pc-settings__input"
            type="password"
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setError(null);
            }}
            placeholder={t('personalContext.authorization.githubTokenPlaceholder')}
            autoFocus
          />
          <div className="pc-settings__field-hint">{t('personalContext.authorization.githubTokenHint')}</div>
        </div>
        {error && <div className="pc-settings__error">{error}</div>}
        <div className="pc-settings__modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            {t('personalContext.services.cancel')}
          </button>
          <button type="button" className="btn primary" onClick={handleSave} disabled={saving}>
            {saving ? t('personalContext.authorization.authorizing') : t('personalContext.authorization.authorize')}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * GitCode PAT 输入弹窗：提交后走后端 authorize_provider 真实校验并落盘（不再用 localStorage mock）。
 */
function GitcodeTokenModal({
  onClose,
  onSave,
}: {
  onClose: () => void;
  onSave: (pat: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [pat, setPat] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    const trimmed = pat.trim();
    if (!trimmed) {
      setError(t('personalContext.authorization.gitcodeTokenRequired'));
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave(trimmed);
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="pc-settings__modal-overlay" onClick={onClose}>
      <div className="pc-settings__modal" onClick={(e) => e.stopPropagation()}>
        <div className="pc-settings__modal-head">
          <h3 className="pc-settings__modal-title">{t('personalContext.authorization.authorize')} · {t('personalContext.provider.gitcode')}</h3>
          <button type="button" className="pc-settings__modal-close" onClick={onClose} aria-label="close">
            <X size={16} />
          </button>
        </div>
        <div className="pc-settings__field">
          <label>{t('personalContext.authorization.gitcodeTokenLabel')}</label>
          <input
            className="pc-settings__input"
            type="password"
            value={pat}
            onChange={(e) => {
              setPat(e.target.value);
              setError(null);
            }}
            placeholder={t('personalContext.authorization.gitcodeTokenPlaceholder')}
            autoFocus
          />
          <div className="pc-settings__field-hint">{t('personalContext.authorization.gitcodeTokenHint')}</div>
        </div>
        {error && <div className="pc-settings__error">{error}</div>}
        <div className="pc-settings__modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            {t('personalContext.services.cancel')}
          </button>
          <button type="button" className="btn primary" onClick={handleSave} disabled={saving}>
            {saving ? t('personalContext.authorization.authorizing') : t('personalContext.authorization.authorize')}
          </button>
        </div>
      </div>
    </div>
  );
}
