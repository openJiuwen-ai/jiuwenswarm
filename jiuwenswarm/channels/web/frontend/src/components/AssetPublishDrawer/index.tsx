import { useEffect, useRef, useState } from 'react';
import { HelpCircle, CircleCheck, Clock3, CircleAlert, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { FormDrawer, Input, Textarea } from '../ui';
import {
  beginHubOAuth,
  getStoredOAuthProvider,
  getStoredOAuthUser,
  waitForHubOAuth,
  type OAuthProvider,
} from '../../utils/gitcodeOAuth';
import { PUBLISH_RESTORE_KEY, useAssetPublish } from '../../hooks/useAssetPublish';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { publishOutcome, validateMetadata, publishTimestamp } from '../../features/assetPublishState';
import { publishIssueKey } from '../../features/assetPublishErrors';
import type { AssetPublishOpenRequest, AssetReference, PublishMetadata } from '../../types/assetPublish';
import gitcodeIcon from '../../assets/settings/channels/gitcode.png';
import githubIcon from '../../assets/settings/channels/GitHub.svg';
import './style.css';

/** 字段提示问号：useAdaptiveTooltip 弹出说明（hook 不能在 map 内调用，抽为子组件） */
function FieldHint({
  text,
  field,
  testId = 'asset-publish-field-hint',
}: {
  text: string;
  field: string;
  testId?: string;
}) {
  const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top' });
  return (
    <>
      <span
        className="asset-publish-hint"
        tabIndex={0}
        aria-label={text}
        data-tooltip={text}
        {...handlers}
        onClick={(event) => {
          if (!event.currentTarget.closest('summary')) return;
          event.preventDefault();
          event.stopPropagation();
        }}
        onKeyDown={(event) => {
          if (event.currentTarget.closest('summary')) event.stopPropagation();
        }}
        data-testid={testId}
        data-variant={field}
      >
        <HelpCircle size={14} aria-hidden="true" />
      </span>
      {tooltip}
    </>
  );
}

const messages = {
  zh: {
    title: '发布资源',
    destination: '发布到',
    accountScope: '记录按当前登录隔离；重新登录后，可见记录可能改变。',
    close: '关闭',
    cancel: '取消',
    asset_name: '发布名称',
    display_name: '展示名称',
    version: '版本',
    description: '简介',
    tags: '标签',
    tagsPlaceholder: '输入多个标签时使用逗号分隔',
    version_desc: '版本说明',
    visibility: '可见范围',
    public: '公开',
    private: '私有',
    target: '更新目标 Hub ID（首次发布留空）',
    ownership: '只有你有更新权限的资源才能作为目标；安装来源不代表所有权。',
    force: '覆盖已有版本（明确确认后开启）',
    prepare: '检查发布内容',
    commit: '确认发布',
    retry: '找回此次提交',
    busy: '处理中…',
    login: '登录后检查并发布',
    check: '发布内容检查',
    files: '文件',
    excluded: '排除项',
    normalizations: '发布副本调整',
    dependencies: '依赖',
    warnings: '提示',
    errors: '需要修正',
    technical: '技术详情',
    records: '发布记录',
    refresh: '刷新本地已知结果',
    queued: '已排队',
    uploading: '上传中',
    pending_moderation: '提交成功，待审核',
    published: '已发布',
    failed: '发布失败',
    unknown: '结果待核实',
    observed: '这里显示后端已知结果，不自动查询远端审核进度。',
    requestFailed: '请求未完成，请重试或重新登录。',
    invalidPluginStructure: '资源包结构不符合 Hub 要求，请检查清单文件和目录结构。',
    pluginNotFound: 'Hub 中找不到要更新的资源，请检查“更新目标 Hub ID”后重试。',
    sessionExchangeFailed: 'Hub 登录结果兑换失败，请重新授权；若仍失败请联系 Hub 管理员。',
    authRequired: '登录状态已失效，请重新登录。',
    resourceNotFound: '找不到本地资源，请确认资源仍然存在。',
    versionConflict: '该版本已提交过，请修改版本；确认需要覆盖时可在高级设置中开启覆盖。',
    hubRequestFailed:
      'Hub 未接受本次发布请求，可能是版本已存在或发布信息不符合 Hub 要求。请检查版本和发布信息后重试；如仍失败，请联系 Hub 管理员。',
    statusFailed: '任务查询暂时失败；后台任务仍可能运行，可刷新恢复。',
    commitUncertain: '提交响应未收到。请找回此次提交或刷新记录，不要创建新提交。',
    commitRejected: '后台拒绝了此次提交。请返回修改并重新检查。',
    expired: '检查结果已过期，请重新检查。',
    invalid: '请检查发布名称、展示名称或版本（1.0.0 / 七位小写提交号）。',
    edit: '返回修改',
    recheck: '检查新版本',
    identity: '远端更新权限由 Hub 最终校验。',
    versionHint: '例如 1.0.0 或 abcdef0',
    loginAgain: '重新登录',
    unavailable: '此资源暂时无法发布，请查看检查原因。',
    empty: '无',
    draftExpiry: '检查结果有效期',
    localSource: '本地资源',
    basicInfo: '基本信息',
    publishSettings: '发布设置',
    advancedSettings: '高级设置',
    advancedHint: '更新已有资源或覆盖版本时使用',
    publicDescription: '审核通过后可在 Hub 中公开展示。',
    privateDescription: '仅对当前发布账号可见。',
    switchAccount: '切换账号',
    loginWith: '使用',
    signedIn: '已登录',
    forceWarning: '将尝试覆盖 Hub 中的同版本内容，请确认你有更新权限。',
    history: '本地发布记录',
    historyHelp: '显示当前账号在本机发起的发布任务，用于查看提交结果或找回超时任务，不代表 Hub 最新审核状态。',
    historyEmpty: '暂无本地发布记录',
    assetId: 'Hub 资产 ID',
    resultVersion: '版本',
    resultTime: '更新时间',
    invalidAssetName: '仅支持小写字母、数字、下划线和连字符，最长 64 个字符。',
    invalidVersion: '请输入 1.0.0 格式版本号或七位小写提交号。',
    invalidDisplayName: '请输入展示名称，最长 128 个字符。',
    assetNameHelp: '发布名称是 Hub 中资源包的技术标识，只支持小写字母、数字、下划线和连字符，最长 64 个字符。',
    displayNameHelp: '展示名称用于 Hub 广场卡片和详情页展示，可以使用中文，最长 128 个字符。',
    descriptionHelp: '简介发布后显示在 Hub 广场卡片和详情页中，用于说明资源的主要能力。',
    tagsHelp: '标签用于 Hub 的分类、搜索和资源识别，多个标签使用逗号分隔。',
  },
  en: {
    title: 'Publish resource',
    destination: 'Publish to',
    accountScope: 'Records are scoped to this sign-in and may change after signing in again.',
    close: 'Close',
    cancel: 'Cancel',
    asset_name: 'Package name',
    display_name: 'Display name',
    version: 'Version',
    description: 'Description',
    tags: 'Tags',
    tagsPlaceholder: 'Separate multiple tags with commas',
    version_desc: 'Release notes',
    visibility: 'Visibility',
    public: 'Public',
    private: 'Private',
    target: 'Target Hub ID (leave empty for a new resource)',
    ownership: 'Only target a resource you can update. An installation source does not establish ownership.',
    force: 'Overwrite an existing version (explicit opt-in)',
    prepare: 'Check package',
    commit: 'Confirm publish',
    retry: 'Recover this submission',
    busy: 'Working…',
    login: 'Sign in to check and publish',
    check: 'Package review',
    files: 'Files',
    excluded: 'Excluded',
    normalizations: 'Snapshot adjustments',
    dependencies: 'Dependencies',
    warnings: 'Warnings',
    errors: 'Fix before publishing',
    technical: 'Technical details',
    records: 'Publishing records',
    refresh: 'Refresh known local result',
    queued: 'Queued',
    uploading: 'Uploading',
    pending_moderation: 'Submitted, pending moderation',
    published: 'Published',
    failed: 'Publishing failed',
    unknown: 'Result needs verification',
    observed: 'This is the result known to the backend; remote moderation is not polled.',
    requestFailed: 'The request did not complete. Retry or sign in again.',
    invalidPluginStructure: 'The package structure does not meet Hub requirements. Check its manifest and folders.',
    pluginNotFound: 'The Hub resource to update was not found. Check the target Hub ID and retry.',
    sessionExchangeFailed:
      'Hub could not exchange the sign-in result. Authorize again or contact the Hub administrator.',
    authRequired: 'Your sign-in has expired. Sign in again.',
    resourceNotFound: 'The local resource was not found. Confirm that it still exists.',
    versionConflict:
      'This version was already submitted. Change it, or explicitly enable overwrite in advanced settings.',
    hubRequestFailed:
      'Hub did not accept this publishing request. The version may already exist, or the publishing information may not meet Hub requirements. Check the version and publishing information, then retry. If the problem continues, contact the Hub administrator.',
    statusFailed: 'Status is temporarily unavailable. The background task may still be running; refresh to recover.',
    commitUncertain:
      'No submission response was received. Recover this submission or refresh records before starting another.',
    commitRejected: 'The backend rejected this submission. Return to editing and check the package again.',
    expired: 'Package review expired. Check the package again.',
    invalid: 'Check the package name, display name and version (1.0.0 or seven lowercase hex characters).',
    edit: 'Back to editing',
    recheck: 'Check a new version',
    identity: 'Hub makes the final decision on update permissions.',
    versionHint: 'For example 1.0.0 or abcdef0',
    loginAgain: 'Sign in again',
    unavailable: 'This resource cannot be published yet. Review the reasons below.',
    empty: 'None',
    draftExpiry: 'Review expires',
    localSource: 'Local resource',
    basicInfo: 'Basic information',
    publishSettings: 'Publishing settings',
    advancedSettings: 'Advanced settings',
    advancedHint: 'For updating an existing resource or overwriting a version',
    publicDescription: 'Visible in Hub after moderation approval.',
    privateDescription: 'Visible only to the publishing account.',
    switchAccount: 'Switch account',
    loginWith: 'Sign in with',
    signedIn: 'signed in',
    forceWarning: 'This will attempt to overwrite the same Hub version. Confirm that you have update permission.',
    history: 'Local publishing history',
    historyHelp:
      'Shows publishing tasks started by this account on this device, for checking results or recovering timed-out submissions. It does not represent the latest Hub moderation status.',
    historyEmpty: 'No local publishing records',
    assetId: 'Hub asset ID',
    resultVersion: 'Version',
    resultTime: 'Updated',
    invalidAssetName: 'Use lowercase letters, numbers, underscores, or hyphens; maximum 64 characters.',
    invalidVersion: 'Enter a version such as 1.0.0 or a seven-character lowercase revision.',
    invalidDisplayName: 'Enter a display name of at most 128 characters.',
    assetNameHelp:
      'The package identifier in Hub. Use lowercase letters, numbers, underscores, or hyphens; maximum 64 characters.',
    displayNameHelp: 'The user-facing name shown on Hub cards and detail pages; maximum 128 characters.',
    descriptionHelp: 'Shown on Hub cards and detail pages to explain the main capabilities of the resource.',
    tagsHelp: 'Used for Hub categories, search, and discovery. Separate multiple tags with commas.',
  },
};
type MessageKey = keyof typeof messages.en;
function hubHost(value?: string): string {
  if (!value) return '';
  try {
    return new URL(value).host;
  } catch {
    return value;
  }
}
const providerIcon = (provider: OAuthProvider) => (provider === 'github' ? githubIcon : gitcodeIcon);
function AssetPublishDrawer({
  reference,
  restored,
  onClose,
}: {
  reference: AssetPublishOpenRequest;
  restored?: PublishMetadata;
  onClose: () => void;
}) {
  const { i18n } = useTranslation();
  const text = (key: MessageKey) => messages[i18n.language.startsWith('zh') ? 'zh' : 'en'][key];
  const publishReference: AssetReference = { kind: reference.kind, local_id: reference.local_id };
  const state = useAssetPublish(publishReference, restored);
  const dateText = (value: string | number | undefined) => {
    const time = publishTimestamp(value);
    return Number.isFinite(time) ? new Date(time).toLocaleString(i18n.language) : '';
  };
  const issueText = (value: unknown) => {
    const issue = value as { code?: string; message?: string; path?: string };
    const code = typeof value === 'string' ? value : issue?.code;
    const zh = i18n.language.startsWith('zh');
    const known: Record<string, [string, string]> = {
      SOURCE_CHANGED: ['源文件已变化，请重新检查。', 'Source files changed. Check the package again.'],
      AUTH_REQUIRED: ['请先登录。', 'Sign in first.'],
      DRAFT_EXPIRED: ['检查结果已过期，请重新检查。', 'Package review expired. Check again.'],
      RESOURCE_NOT_FOUND: [
        '找不到本地资源，请检查是否已安装。',
        'Local resource was not found. Check its installation.',
      ],
      SECRET_DETECTED: [
        '包内检测到敏感信息，请移除后重新检查。',
        'Sensitive information was detected. Remove it and check again.',
      ],
      VERSION_CONFLICT: [
        '版本已存在，请修改版本或明确选择覆盖。',
        'This version exists. Change the version or explicitly enable overwrite.',
      ],
    };
    if (code && known[code]) return known[code][zh ? 0 : 1] + (issue?.path ? ` (${issue.path})` : '');
    const safeKey = publishIssueKey(code || value);
    return safeKey === 'requestFailed' ? text('requestFailed') : text(safeKey);
  };
  const panel = useRef<HTMLElement | null>(null);
  const [review, setReview] = useState(false);
  const [switchingAccount, setSwitchingAccount] = useState(false);
  const [loginError, setLoginError] = useState('');
  const [loginBusy, setLoginBusy] = useState(false);
  const loginAbort = useRef<AbortController | null>(null);
  const loginWindow = useRef<Window | null>(null);
  const loginGeneration = useRef(0);
  useEffect(() => () => loginAbort.current?.abort(), []);
  const [avatarFailed, setAvatarFailed] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  useEffect(() => {
    if (state.targetAssetId || state.force) setAdvancedOpen(true);
  }, [state.targetAssetId, state.force]);
  useEffect(() => {
    if (state.submissionLocked || state.error === 'commitUncertain') setHistoryOpen(true);
  }, [state.submissionLocked, state.error]);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
      if (event.key !== 'Tab') return;
      const controls = panel.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, [tabindex="0"]',
      );
      if (!controls?.length) return;
      const first = controls[0],
        last = controls[controls.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', keydown);
    return () => {
      document.removeEventListener('keydown', keydown);
      previous?.focus();
    };
  }, [onClose]);
  const login = async (provider: OAuthProvider) => {
    loginAbort.current?.abort();
    loginWindow.current?.close();
    const generation = ++loginGeneration.current;
    const desktopOpen = window.pywebview?.api?.open_external_url;
    const browserTab = desktopOpen || window.__JIUWEN_DESKTOP__ ? null : window.open('', '_blank');
    loginWindow.current = browserTab;
    try {
      setLoginError('');
      setLoginBusy(true);
      if (window.__JIUWEN_DESKTOP__ && !desktopOpen) throw new Error('桌面浏览器功能尚未就绪，请重试。');
      if (!desktopOpen && !browserTab) throw new Error('浏览器阻止了授权窗口，请允许打开新标签页。');
      if (browserTab) browserTab.opener = null;
      const attempt = await beginHubOAuth(provider);
      if (desktopOpen) {
        const opened = await desktopOpen(attempt.authorize_url);
        if (!opened) throw new Error('无法打开系统浏览器，请重试。');
      } else if (browserTab) browserTab.location.href = attempt.authorize_url;
      loginAbort.current?.abort();
      const controller = new AbortController();
      loginAbort.current = controller;
      await waitForHubOAuth(attempt, controller.signal, () => Boolean(browserTab?.closed));
      if (generation !== loginGeneration.current) return;
      setSwitchingAccount(false);
    } catch (failure) {
      browserTab?.close();
      if (failure instanceof DOMException && failure.name === 'AbortError') return;
      if (generation !== loginGeneration.current) return;
      setLoginError(failure instanceof Error ? failure.message : 'OAuth 登录不可用');
    } finally {
      if (generation === loginGeneration.current) {
        loginWindow.current = null;
        setLoginBusy(false);
      }
    }
  };
  const invalid = validateMetadata(state.metadata).length > 0;
  const invalidFields = new Set(validateMetadata(state.metadata));
  const fields = ['asset_name', 'version', 'display_name', 'description', 'tags', 'version_desc'] as const;
  const fieldHints: Partial<Record<(typeof fields)[number], MessageKey>> = {
    asset_name: 'assetNameHelp',
    display_name: 'displayNameHelp',
    description: 'descriptionHelp',
    tags: 'tagsHelp',
  };
  const outcome = state.record ? publishOutcome(state.record) : null;
  const ResultIcon =
    outcome === 'failed'
      ? CircleAlert
      : ['queued', 'uploading', 'unknown'].includes(outcome || '')
        ? Clock3
        : CircleCheck;
  const zh = i18n.language.startsWith('zh');
  const provider = getStoredOAuthProvider();
  const oauthUser = getStoredOAuthUser();
  const providerName = provider === 'github' ? 'GitHub' : 'GitCode';
  const accountName = oauthUser?.name || oauthUser?.login || `${providerName} ${text('signedIn')}`;
  const assetType = {
    skill: 'Skill',
    agent_template: zh ? '专家' : 'Expert',
    agent_group: zh ? '专家团' : 'Expert group',
    plugin: zh ? '插件' : 'Plugin',
    mcp: zh ? '连接器' : 'Connector',
  }[reference.kind];
  const invalidText: Partial<Record<(typeof fields)[number], MessageKey>> = {
    asset_name: 'invalidAssetName',
    version: 'invalidVersion',
    display_name: 'invalidDisplayName',
  };
  const renderField = (field: (typeof fields)[number], className = '') => (
    <label
      key={field}
      className={`asset-publish-field ${className}`.trim()}
      data-testid="asset-publish-field"
      data-variant={field}
    >
      <span>
        {text(field)}
        {['asset_name', 'display_name', 'version'].includes(field) && (
          <span className="asset-publish-required" aria-hidden="true">
            {' '}
            *
          </span>
        )}
        {fieldHints[field] && <FieldHint text={text(fieldHints[field])} field={field} />}
      </span>
      {field === 'description' || field === 'version_desc' ? (
        <Textarea
          data-testid={`asset-publish-${field.replaceAll('_', '-')}`}
          value={state.metadata[field]}
          onChange={(value) => state.edit({ [field]: value })}
          rows={3}
        />
      ) : (
        <Input
          data-testid={`asset-publish-${field.replaceAll('_', '-')}`}
          value={field === 'tags' ? state.metadata.tags.join(', ') : state.metadata[field]}
          onChange={(value) =>
            state.edit({
              [field]: field === 'tags' ? value.split(',').map((item) => item.trim()) : value,
            })
          }
          required={['asset_name', 'display_name', 'version'].includes(field)}
          placeholder={
            field === 'version' ? text('versionHint') : field === 'tags' ? text('tagsPlaceholder') : undefined
          }
          invalid={invalidFields.has(field)}
        />
      )}
      {invalidFields.has(field) && invalidText[field] && (
        <span className="asset-publish-field-error" data-testid="asset-publish-field-error">
          {text(invalidText[field])}
        </span>
      )}
    </label>
  );
  return (
    <FormDrawer
      title={text('title')}
      onClose={onClose}
      testId="asset-publish-drawer"
      className="asset-publish-drawer"
      panelRef={panel}
      closeTestId="asset-publish-close"
      footer={
        <>
          {!review && !state.record && invalid && (
            <p className="asset-publish-footer-validation" data-testid="asset-publish-validation">
              {text('invalid')}
            </p>
          )}
          {!review && !state.record && (
            <>
              <button type="button" onClick={onClose} data-testid="asset-publish-cancel">
                {text('cancel')}
              </button>
              <button
                type="submit"
                form="asset-publish-form"
                className="asset-publish-primary"
                data-testid="asset-publish-prepare"
                disabled={invalid || state.busy || !state.loggedIn || !state.description?.can_publish}
              >
                {text(state.busy ? 'busy' : 'prepare')}
              </button>
            </>
          )}
          {review && !state.record && (
            <>
              <button
                type="button"
                data-testid="asset-publish-edit"
                disabled={state.busy || state.attempted}
                onClick={() => {
                  state.edit({});
                  setReview(false);
                }}
              >
                {text('edit')}
              </button>
              <button
                type="button"
                className="asset-publish-primary"
                data-testid="asset-publish-commit"
                disabled={state.busy || !state.draft?.can_submit || !!state.draft?.errors?.length || !state.loggedIn}
                onClick={() => void state.commit()}
              >
                {text(state.busy ? 'busy' : state.attempted ? 'retry' : 'commit')}
              </button>
            </>
          )}
          {state.record && (
            <button type="button" onClick={onClose} data-testid="asset-publish-done">
              {text('close')}
            </button>
          )}
        </>
      }
    >
      <section className="asset-publish-resource-summary" data-testid="asset-publish-resource-summary">
        <span className="asset-publish-resource-avatar" aria-hidden="true">
          {reference.avatar_url && !avatarFailed ? (
            <img
              src={reference.avatar_url}
              alt=""
              data-testid="asset-publish-resource-avatar-image"
              onError={() => setAvatarFailed(true)}
            />
          ) : (
            (state.metadata.display_name || reference.local_id).trim().charAt(0).toUpperCase()
          )}
        </span>
        <div className="asset-publish-resource-copy">
          <strong>{state.metadata.display_name || reference.local_id}</strong>
          <div className="asset-publish-resource-meta">
            <span>{assetType}</span>
            <span>{text('localSource')}</span>
          </div>
          <span
            className="asset-publish-resource-id"
            data-testid="asset-publish-resource-id"
            title={reference.local_id}
          >
            {reference.local_id}
          </span>
        </div>
      </section>
      {state.loggedIn ? (
        <section className="asset-publish-account-card" data-testid="asset-publish-account-card">
          <div className="asset-publish-account-copy">
            <strong className="asset-publish-provider-identity">
              <img
                src={providerIcon(provider)}
                alt=""
                aria-hidden="true"
                data-testid="asset-publish-provider-icon"
                data-variant={provider}
              />
              {providerName}
            </strong>
            <span>{accountName}</span>
            {state.description?.hub_url && (
              <span data-testid="asset-publish-destination">{hubHost(state.description.hub_url)}</span>
            )}
          </div>
          <button
            type="button"
            className="asset-publish-text-button"
            data-testid="asset-publish-switch-account"
            aria-expanded={switchingAccount}
            onClick={() => setSwitchingAccount((value) => !value)}
          >
            {text('switchAccount')}
          </button>
          {switchingAccount && (
            <div className="asset-publish-provider-options">
              {(['gitcode', 'github'] as const).map((nextProvider) => (
                <button
                  type="button"
                  key={nextProvider}
                  data-testid="asset-publish-provider"
                  data-variant={nextProvider}
                  aria-busy={loginBusy}
                  onClick={() => login(nextProvider)}
                  disabled={state.busy}
                >
                  <img src={providerIcon(nextProvider)} alt="" aria-hidden="true" />
                  {nextProvider === 'github' ? 'GitHub' : 'GitCode'}
                </button>
              ))}
            </div>
          )}
          {state.description?.identity_verified === false && (
            <span className="asset-publish-account-note" data-testid="asset-publish-account-scope">
              {text('accountScope')}
            </span>
          )}
        </section>
      ) : (
        <section className="asset-publish-login" data-testid="asset-publish-login">
          <span>{text('login')}</span>
          {(['gitcode', 'github'] as const).map((nextProvider) => (
            <button
              type="button"
              key={nextProvider}
              data-testid="asset-publish-provider"
              data-variant={nextProvider}
              aria-busy={loginBusy}
              onClick={() => login(nextProvider)}
              disabled={state.busy}
            >
              <img src={providerIcon(nextProvider)} alt="" aria-hidden="true" />
              {text('loginWith')} {nextProvider === 'github' ? 'GitHub' : 'GitCode'}
            </button>
          ))}
        </section>
      )}
      {loginError && (
        <p role="alert" className="text-danger">
          {loginError}
        </p>
      )}
      {state.error && (
        <p role="alert" className="text-danger" data-testid="asset-publish-error">
          {text(state.error as MessageKey)}
        </p>
      )}
      {!review && !state.record && (
        <form
          id="asset-publish-form"
          data-testid="asset-publish-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!invalid) {
              setReview(true);
              void state.prepare();
            }
          }}
        >
          <fieldset
            className="asset-publish-form-fields"
            disabled={state.busy || (state.loggedIn && !state.description)}
          >
            <section
              className="asset-publish-form-section"
              data-testid="asset-publish-form-section"
              data-variant="basic"
            >
              <h3>{text('basicInfo')}</h3>
              <div className="asset-publish-field-row">
                {renderField('asset_name')}
                {renderField('version')}
              </div>
              {renderField('display_name')}
              {renderField('description')}
              {renderField('tags')}
            </section>
            <section
              className="asset-publish-form-section"
              data-testid="asset-publish-form-section"
              data-variant="publish"
            >
              <h3>{text('publishSettings')}</h3>
              <div className="asset-publish-field">
                <span data-testid="asset-publish-visibility-label">{text('visibility')}</span>
                <div className="asset-publish-visibility-options" role="radiogroup" aria-label={text('visibility')}>
                  {(['public', 'private'] as const).map((visibility) => (
                    <label
                      key={visibility}
                      className="asset-publish-visibility-option"
                      data-testid="asset-publish-visibility-option"
                      data-selected={state.metadata.visibility === visibility}
                    >
                      <input
                        type="radio"
                        name="asset-publish-visibility"
                        value={visibility}
                        checked={state.metadata.visibility === visibility}
                        onChange={() => state.edit({ visibility })}
                      />
                      <span>
                        <strong>{text(visibility)}</strong>
                        <small>{text(visibility === 'public' ? 'publicDescription' : 'privateDescription')}</small>
                      </span>
                    </label>
                  ))}
                </div>
              </div>
              {renderField('version_desc')}
            </section>
            <details
              className="asset-publish-advanced"
              data-testid="asset-publish-form-section"
              data-variant="advanced"
              open={advancedOpen}
              onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
            >
              <summary>
                <span>{text('advancedSettings')}</span>
                <FieldHint text={text('advancedHint')} field="advanced" testId="asset-publish-advanced-help" />
              </summary>
              <div className="asset-publish-advanced-content">
                <label className="asset-publish-field">
                  <span data-testid="asset-publish-target-label">{text('target')}</span>
                  <Input
                    data-testid="asset-publish-target"
                    value={state.targetAssetId}
                    onChange={(value) => state.setTargetAssetId(value)}
                  />
                </label>
                <p className="text-text-muted" data-testid="asset-publish-ownership-note">
                  {text('ownership')}
                </p>
                <label className="asset-publish-checkbox">
                  <input
                    type="checkbox"
                    role="switch"
                    data-testid="asset-publish-force"
                    checked={state.force}
                    onChange={(event) => state.setForce(event.target.checked)}
                  />
                  {text('force')}
                </label>
                {state.force && <p className="asset-publish-force-warning">{text('forceWarning')}</p>}
              </div>
            </details>
          </fieldset>
          {state.description && !state.description.can_publish && (
            <p role="alert" data-testid="asset-publish-unavailable">
              {text('unavailable')}
            </p>
          )}
          {state.description?.errors?.map((issue, index) => (
            <pre className="text-danger" data-testid="asset-publish-description-error" data-variant={index} key={index}>
              {issueText(issue)}
            </pre>
          ))}
        </form>
      )}
      {review && !state.record && (
        <section data-testid="asset-publish-review">
          <h3 data-testid="asset-publish-review-title">{text('check')}</h3>
          <p data-testid="asset-publish-review-visibility">
            {text('visibility')}: {text(state.metadata.visibility)}
          </p>
          {state.draft && (
            <>
              <p data-testid="asset-publish-package-summary">
                {state.draft.package_name} · {state.draft.version} · {state.draft.size_bytes} bytes
              </p>
              {(['files', 'excluded', 'normalizations', 'dependencies', 'warnings', 'errors'] as const).map((key) => (
                <details
                  key={key}
                  open={key === 'errors' || key === 'warnings'}
                  data-testid="asset-publish-review-section"
                  data-variant={key}
                >
                  <summary>
                    {text(key)} ({state.draft?.[key]?.length || 0})
                  </summary>
                  {(state.draft?.[key] || []).map((item, index) => (
                    <pre key={index} data-testid="asset-publish-review-item" data-variant={`${key}-${index}`}>
                      {issueText(item)}
                    </pre>
                  ))}
                </details>
              ))}
              <details data-testid="asset-publish-technical">
                <summary>{text('technical')}</summary>
                <pre>{state.draft.checksum_sha256 || state.draft.artifact_sha256}</pre>
                <p>
                  {text('draftExpiry')}: {dateText(state.draft.expires_at)}
                </p>
              </details>
            </>
          )}
        </section>
      )}
      {state.record && outcome && (
        <section
          aria-live="polite"
          className="asset-publish-result-card"
          data-testid="asset-publish-result"
          data-variant={outcome}
        >
          <header className="asset-publish-result-header" data-testid="asset-publish-result-header">
            <span className="asset-publish-result-icon" aria-hidden="true">
              <ResultIcon size={20} />
            </span>
            <h3>{text(outcome as MessageKey)}</h3>
          </header>
          <dl className="asset-publish-result-metadata" data-testid="asset-publish-result-metadata">
            {state.record.result?.asset_id && (
              <div>
                <dt>{text('assetId')}</dt>
                <dd title={state.record.result.asset_id}>{state.record.result.asset_id}</dd>
              </div>
            )}
            <div>
              <dt>{text('resultVersion')}</dt>
              <dd>{state.record.result?.version || state.record.version || text('empty')}</dd>
            </div>
            {state.record.updated_at && (
              <div>
                <dt>{text('resultTime')}</dt>
                <dd>{dateText(state.record.updated_at)}</dd>
              </div>
            )}
          </dl>
          {state.record.error && (
            <p className="asset-publish-result-error text-danger">{issueText(state.record.error)}</p>
          )}
          <footer className="asset-publish-result-footer" data-testid="asset-publish-result-footer">
            <p data-testid="asset-publish-observed-note">{text('observed')}</p>
            {!['queued', 'uploading', 'unknown'].includes(outcome) && (
              <button
                type="button"
                data-testid="asset-publish-new-version"
                disabled={state.submissionLocked || state.busy}
                onClick={() => {
                  state.edit({});
                  setReview(false);
                }}
              >
                {text('recheck')}
              </button>
            )}
          </footer>
        </section>
      )}
      {state.loggedIn && (
        <details
          className="asset-publish-records-fold"
          data-testid="asset-publish-records-fold"
          open={historyOpen}
          onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
        >
          <summary>
            <span>{text('history')}</span>
            <FieldHint text={text('historyHelp')} field="records" testId="asset-publish-records-help" />
          </summary>
          <section data-testid="asset-publish-records">
            <div className="asset-publish-records-toolbar" data-testid="asset-publish-records-toolbar">
              <button
                type="button"
                data-testid="asset-publish-refresh"
                disabled={state.busy}
                onClick={() => void state.refresh()}
              >
                <RefreshCw size={13} aria-hidden="true" />
                {text('refresh')}
              </button>
            </div>
            {state.records.length === 0 && <p className="asset-publish-records-empty">{text('historyEmpty')}</p>}
            {state.records.map((record) => (
              <button
                className="asset-publish-record"
                type="button"
                key={record.operation_id}
                data-testid="asset-publish-record"
                disabled={state.submissionLocked || state.busy}
                data-variant={record.operation_id}
                onClick={() => state.setRecord(record)}
              >
                <span className="asset-publish-record-main">
                  <strong>{record.result?.version || record.version || text('empty')}</strong>
                  {record.updated_at && <small>{dateText(record.updated_at)}</small>}
                </span>
                <span className="asset-publish-record-status" data-variant={publishOutcome(record)}>
                  {text(publishOutcome(record) as MessageKey)}
                </span>
              </button>
            ))}
          </section>
        </details>
      )}
    </FormDrawer>
  );
}
export function AssetPublishHost() {
  const [selection, setSelection] = useState<{ reference: AssetPublishOpenRequest; metadata?: PublishMetadata } | null>(
    null,
  );
  useEffect(() => {
    const open = (event: Event) => setSelection({ reference: (event as CustomEvent<AssetPublishOpenRequest>).detail });
    const restore = () => {
      try {
        const value = sessionStorage.getItem(PUBLISH_RESTORE_KEY);
        if (!value) return;
        const saved = JSON.parse(value);
        if (
          ['skill', 'agent_template', 'agent_group', 'plugin', 'mcp'].includes(saved.reference?.kind) &&
          typeof saved.reference?.local_id === 'string'
        )
          setSelection(saved);
        sessionStorage.removeItem(PUBLISH_RESTORE_KEY);
      } catch {
        sessionStorage.removeItem(PUBLISH_RESTORE_KEY);
      }
    };
    restore();
    window.addEventListener('asset-publish-open', open);
    window.addEventListener('oauth-callback-complete', restore);
    return () => {
      window.removeEventListener('asset-publish-open', open);
      window.removeEventListener('oauth-callback-complete', restore);
    };
  }, []);
  return selection ? (
    <AssetPublishDrawer
      key={`${selection.reference.kind}:${selection.reference.local_id}`}
      reference={selection.reference}
      restored={selection.metadata}
      onClose={() => setSelection(null)}
    />
  ) : null;
}
