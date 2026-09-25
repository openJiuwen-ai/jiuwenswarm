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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, X } from 'lucide-react';
import { Switch } from '../Switch';
import { SettingRow } from '../../features/settings/components/SettingRow';
import ModelPicker from '../ModelPicker';
import { usePersonalContextStore } from '../../stores';
import { useSessionStore } from '../../stores';
import {
  STRATEGY_OPTIONS,
  hasRunningFetchTask,
  isFetchTaskRunningError,
  pcApi,
} from '../../services/personalContextApi';
import { toast } from '../../components/ui/Toast/toastStore';
import './SettingsPanel.css';
import feishuLogo from '../../assets/settings/channels/feishu.svg';
import githubLogo from '../../assets/settings/channels/GitHub.svg';
import gitcodeLogo from '../../assets/settings/channels/gitcode.png';
interface PersonalContextSettingsPanelProps {
  isConnected: boolean;
}

/** webClient 请求超时错误（code=REQUEST_TIMEOUT）。 */
function isRequestTimeoutError(error: unknown): boolean {
  return String((error as { code?: unknown })?.code ?? '') === 'REQUEST_TIMEOUT';
}

/** 后端 PAT 校验失败类错误（token 无效 / 校验超时 / 请求失败），统一转友好提示。 */
function isRepositoryCredentialError(error: unknown): boolean {
  return /repository provider credential validation/i.test(
    error instanceof Error ? error.message : String(error),
  );
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
    configNeedsReconciliation,
    loadAll,
    loadStatus,
    setMasterEnabled,
    setEnabled,
    setStrategyProfile,
    selectModel,
    loadAuthStatus,
    authorizeProvider,
    authByProvider,
  } = usePersonalContextStore();
  const availableModels = useSessionStore((s) => s.availableModels);
  // 上下文整理模型只允许选择已配置模型，登录/免费模型由网关按会话注入凭据，
  // 无法被 personal_context 持久化保存，选中会触发后端下标越界，这里直接过滤掉。
  const visibleModels = availableModels.filter((m) => m.is_free !== true);
  const currentModelName =
    config.model_index != null ? availableModels[config.model_index]?.model_name ?? null : null;

  // 总开关为独立持久化状态；兼容旧配置缺失时按子开关派生兜底。
  const masterEnabled = config.master_enabled ?? (config.collection_enabled || config.agent_use_enabled);

  const [error, setError] = useState<string | null>(null);
  const [githubModalOpen, setGithubModalOpen] = useState(false);
  const [gitcodeModalOpen, setGitcodeModalOpen] = useState(false);

  // 后端 stored_config 落盘后不带 configured 字段，只有 PersonalContextStatus 稳定带。
  // 因此"是否已配置"以 status.configured 为准，而非 config.configured。
  const isConfigured = status?.configured === true || config.collection_enabled === true;
  const fetchActive = hasRunningFetchTask(status);

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

  useEffect(() => {
    if (!isConnected) return;
    const id = window.setInterval(() => void loadStatus().catch(() => {}), 5000);
    return () => window.clearInterval(id);
  }, [isConnected, loadStatus]);

  // 请求超时后由 store 核对实际配置；核对期间开关保持请求态。
  const runWrite = useCallback(
    (op: () => Promise<void>): Promise<void> =>
      op().catch((e: unknown) => {
        if (isRequestTimeoutError(e)) {
          toast.open({
            content: t(
              usePersonalContextStore.getState().configNeedsReconciliation
                ? 'personalContext.settings.collectionTimeoutReconciling'
                : 'personalContext.settings.operationTimeout',
            ),
            variant: 'warning',
          });
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
      }),
    [t],
  );

  // 开启采集前确认任务已真正停完：若后端还在跑/停，start 会排在 _operation_lock 后面干等
  // 两个请求串行容易触发前端超时，这里提前拦截。
  const isFetchStillRunning = useCallback(async (): Promise<boolean> => {
    try {
      const fresh = await pcApi.getStatus();
      return hasRunningFetchTask(fresh);
    } catch {
      return false;
    }
  }, []);

  const handleEnabled = useCallback(
    async (enabled: boolean) => {
      setError(null);
      if (!enabled) {
        if (hasRunningFetchTask(status)) {
          toast.open({ content: t('personalContext.settings.collectionStopPending'), variant: 'warning' });
        }
        await runWrite(() => setEnabled(false));
        return;
      }
      if (await isFetchStillRunning()) {
        toast.open({ content: t('personalContext.settings.collectionStoppingRetry'), variant: 'warning' });
        return;
      }
      await runWrite(() => setEnabled(true));
    },
    [isFetchStillRunning, runWrite, setEnabled, status, t],
  );

  const handleMasterEnabled = useCallback(
    async (enabled: boolean) => {
      setError(null);
      if (!enabled) {
        if (hasRunningFetchTask(status)) {
          toast.open({ content: t('personalContext.settings.collectionStopPending'), variant: 'warning' });
        }
        await runWrite(() => setMasterEnabled(false));
        return;
      }
      if (await isFetchStillRunning()) {
        toast.open({ content: t('personalContext.settings.collectionStoppingRetry'), variant: 'warning' });
        return;
      }
      await runWrite(() => setMasterEnabled(true));
    },
    [isFetchStillRunning, runWrite, setMasterEnabled, status, t],
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
        if (isFetchTaskRunningError(e)) {
          void loadStatus().catch(() => {});
          toast.open({ content: t('personalContext.services.fetchTaskRunning'), variant: 'warning' });
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [config.model_index, loadStatus, setStrategyProfile, t],
  );

  const handleModel = useCallback(
    (index: number) => {
      setError(null);
      void selectModel(index).catch((e: unknown) => {
        if (isFetchTaskRunningError(e)) {
          toast.open({ content: t('personalContext.services.fetchTaskRunning'), variant: 'warning' });
          return;
        }
        setError(e instanceof Error ? e.message : String(e));
      });
    },
    [selectModel, t],
  );

  const feishuAuth = authByProvider.feishu;
  const feishuState = feishuAuth?.state ?? 'not_authorized';
  const githubState = authByProvider.github?.state ?? 'not_authorized';
  const gitcodeState = authByProvider.gitcode?.state ?? 'not_authorized';

  // 飞书授权流程标记：step1=首次应用配置中，step2=第 1 步完成/第 2 步授权中，
  // null=非首次单步授权或无进行中的流程（不显示分步徽标）。
  const [feishuFlowStep, setFeishuFlowStep] = useState<'step1' | 'step2' | null>(null);

  const handleFeishuAuthorize = useCallback(() => {
    setError(null);
    // 第 1 步（config_init）完成后的再次点击即第 2 步，保留流程标记；
    // 其余场景（含非首次单步授权）重置为 null，不再显示分步徽标。
    const resumingStep2 = feishuFlowStep === 'step2';
    if (!resumingStep2) setFeishuFlowStep(null);
    void authorizeProvider('feishu', undefined, feishuState === 'authorized')
      .then((result) => {
        if (result?.state === 'authorizing' && result.verification_url) {
          if (result.authorization_step === 'config_init') {
            setFeishuFlowStep('step1');
          } else if (!resumingStep2) {
            // 非首次授权：只有一次登录授权，不显示「第 2 步」徽标
            setFeishuFlowStep(null);
          }
          const win = window.open(result.verification_url, '_blank', 'noopener,noreferrer');
          // 弹窗被拦截时，卡片提示条里的兜底链接可手动打开授权页
          if (!win) {
            toast.open({ content: t('personalContext.authorization.feishuOpenLink'), variant: 'warning' });
          }
        } else if (result?.state === 'authorized') {
          setFeishuFlowStep(null);
          toast.open({ content: t('personalContext.authorization.feishuAuthSuccess'), variant: 'success' });
        } else {
          setFeishuFlowStep(null);
          // 发起即失败（如 lark-cli 缺失/不可用）：直接提示，不留空白页
          toast.open({
            content:
              result?.authorization_step === 'config_init'
                ? t('personalContext.authorization.feishuConfigInitFailed')
                : t('personalContext.authorization.feishuAuthFailed'),
            variant: 'error',
          });
        }
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
      });
  }, [authorizeProvider, feishuFlowStep, feishuState, t]);

  // 授权链接有效期倒计时（仅授权中显示）
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (feishuState !== 'authorizing') return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [feishuState]);

  const feishuExpiresInText = useMemo(() => {
    const expiresAt = feishuAuth?.expires_at;
    if (!expiresAt) return null;
    const remainingSec = Math.max(0, Math.round((new Date(expiresAt).getTime() - now) / 1000));
    if (remainingSec <= 0) return t('personalContext.authorization.feishuAuthExpiredShort');
    if (remainingSec < 60) return t('personalContext.authorization.feishuExpiresInSeconds', { count: remainingSec });
    return t('personalContext.authorization.feishuExpiresInMinutes', { count: Math.ceil(remainingSec / 60) });
  }, [feishuAuth?.expires_at, now, t]);

  // 飞书授权完成/失败的一次性反馈 + 第 1 步完成标记（authorizing → authorized / failed / not_authorized）
  const prevFeishuAuth = useRef({ state: feishuState, step: feishuAuth?.authorization_step ?? null });
  useEffect(() => {
    const prev = prevFeishuAuth.current;
    if (prev.state === 'authorizing' && feishuState === 'authorized') {
      toast.open({ content: t('personalContext.authorization.feishuAuthSuccess'), variant: 'success' });
      setFeishuFlowStep(null);
    } else if (prev.state === 'authorizing' && feishuState === 'authorization_failed') {
      const expired = feishuAuth?.expires_at ? Date.now() > new Date(feishuAuth.expires_at).getTime() : false;
      toast.open({
        content: expired ? t('personalContext.authorization.feishuAuthExpired') : t('personalContext.authorization.feishuAuthFailed'),
        variant: 'error',
      });
      setFeishuFlowStep(null);
    } else if (
      prev.state === 'authorizing' &&
      prev.step === 'config_init' &&
      feishuState === 'not_authorized'
    ) {
      // 第 1 步（应用配置）已完成，保留提示引导第 2 步登录授权
      setFeishuFlowStep('step2');
    }
    prevFeishuAuth.current = { state: feishuState, step: feishuAuth?.authorization_step ?? null };
  }, [feishuState, feishuAuth?.expires_at, feishuAuth?.authorization_step, t]);

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
      <div className="pc-settings__card" data-testid="personal-context-settings-card">
        {/* 总开关 */}
        <SettingRow
          title={t('personalContext.settings.masterEnable')}
          description={t(
            configNeedsReconciliation
              ? 'personalContext.settings.collectionReconciling'
              : 'personalContext.settings.masterEnableHint',
          )}
        >
          <Switch
            checked={masterEnabled}
            onChange={handleMasterEnabled}
            disabled={
              !isConnected ||
              configNeedsReconciliation ||
              !!pendingWrites.collection_enabled ||
              !!pendingWrites.agent_use_enabled
            }
          />
        </SettingRow>

        {masterEnabled && (
          <>
            {/* 采集个人上下文内容 */}
            <SettingRow
              title={t('personalContext.settings.enable')}
              description={t(
                configNeedsReconciliation
                  ? 'personalContext.settings.collectionReconciling'
                  : 'personalContext.settings.enableHint',
              )}
            >
              <Switch
                checked={config.collection_enabled}
                onChange={handleEnabled}
                disabled={!isConnected || configNeedsReconciliation || !!pendingWrites.collection_enabled}
              />
            </SettingRow>

            {config.collection_enabled && (
              <>
            {/* 上下文采集模式 */}
            <SettingRow
              title={t('personalContext.settings.strategyProfile')}
              description={t(fetchActive
                ? 'personalContext.settings.strategyLockedByFetch'
                : 'personalContext.settings.subtitle')}
            >
              <select
                className="pc-settings__select"
                data-testid="personal-context-strategy-select"
                value={config.strategy_profile}
                onChange={(e) => handleStrategy(e.target.value as 'rules' | 'balanced' | 'agent')}
                disabled={!isConnected || fetchActive || !!pendingWrites.strategy_profile}
              >
                {STRATEGY_OPTIONS.map((s) => (
                  <option key={s} value={s}>{t('personalContext.settings.strategy_' + s)}</option>
                ))}
              </select>
            </SettingRow>

            {/* 上下文整理模型 */}
            <SettingRow
              title={t('personalContext.settings.model')}
              description={t('personalContext.settings.modelHint')}
            >
              <ModelPicker
                testIdPrefix="personal-context-model"
                excludeFreeModels
                value={currentModelName}
                onChange={(modelName) => {
                  const idx = availableModels.findIndex((m) => m.model_name === modelName);
                  if (idx >= 0) handleModel(idx);
                }}
                disabled={!isConnected || !!pendingWrites.model_index || visibleModels.length === 0}
              />
            </SettingRow>

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
                {(() => {
                  const authorizing = feishuState === 'authorizing' && !!feishuAuth?.verification_url;
                  const step2Pending = feishuFlowStep === 'step2' && feishuState === 'not_authorized';
                  const configInitActive = authorizing && feishuAuth?.authorization_step === 'config_init';
                  if (!authorizing && !step2Pending) return null;
                  return (
                    <div className="pc-settings__auth-hint" role="status">
                      {(configInitActive || feishuFlowStep !== null) && (
                        <span className="pc-settings__auth-step">
                          {configInitActive
                            ? t('personalContext.authorization.feishuStep1')
                            : t('personalContext.authorization.feishuStep2')}
                        </span>
                      )}
                      <span>
                        {configInitActive
                          ? t('personalContext.authorization.feishuConfigInitHint')
                          : step2Pending
                            ? t('personalContext.authorization.feishuStep1Done')
                            : t('personalContext.authorization.feishuVerifyHint')}
                      </span>
                      {authorizing && (
                        <>
                          <a
                            className="pc-settings__auth-link"
                            href={feishuAuth?.verification_url ?? undefined}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {t('personalContext.authorization.feishuOpenLink')}
                          </a>
                          {feishuExpiresInText && (
                            <span className="pc-settings__auth-expires">{feishuExpiresInText}</span>
                          )}
                        </>
                      )}
                    </div>
                  );
                })()}
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
      setError(
        isRepositoryCredentialError(e)
          ? t('personalContext.authorization.tokenInvalid')
          : e instanceof Error
            ? e.message
            : String(e),
      );
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
      setError(
        isRepositoryCredentialError(e)
          ? t('personalContext.authorization.tokenInvalid')
          : e instanceof Error
            ? e.message
            : String(e),
      );
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
