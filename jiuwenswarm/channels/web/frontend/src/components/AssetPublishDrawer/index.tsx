import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { X, Info, HelpCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { buildOAuthUrl, type OAuthProvider } from '../../utils/gitcodeOAuth';
import { PUBLISH_RESTORE_KEY, useAssetPublish } from '../../hooks/useAssetPublish';
import { publishOutcome, validateMetadata, publishTimestamp } from '../../features/assetPublishState';
import type { AssetReference, PublishMetadata } from '../../types/assetPublish';
import './style.css';
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
    tags: '标签（逗号分隔）',
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
    review: '请核对可见范围与内容，确认后由后台上传。',
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
    tags: 'Tags (comma separated)',
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
    review: 'Check visibility and package contents. Confirming starts the backend upload.',
  },
};
type MessageKey = keyof typeof messages.en;
function display(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}
function AssetPublishDrawer({
  reference,
  restored,
  onClose,
}: {
  reference: AssetReference;
  restored?: PublishMetadata;
  onClose: () => void;
}) {
  const { t, i18n } = useTranslation();
  const text = (key: MessageKey) => messages[i18n.language.startsWith('zh') ? 'zh' : 'en'][key];
  const state = useAssetPublish(reference, restored);
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
    return code && known[code] ? known[code][zh ? 0 : 1] + (issue?.path ? ` (${issue.path})` : '') : display(value);
  };
  const panel = useRef<HTMLElement>(null);
  const [review, setReview] = useState(false);
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
  const login = (provider: OAuthProvider) => {
    sessionStorage.setItem(PUBLISH_RESTORE_KEY, JSON.stringify({ reference, metadata: state.metadata }));
    window.location.href = buildOAuthUrl(provider);
  };
  const invalid = validateMetadata(state.metadata).length > 0;
  const fields = ['asset_name', 'version', 'display_name', 'description', 'tags', 'version_desc'] as const;
  const fieldHints: Record<string, string> = {
    asset_name: 'skillNameTooltip',
    display_name: 'displayNameTooltip',
    description: 'descriptionTooltip',
    tags: 'tagsTooltip',
  };
  const outcome = state.record ? publishOutcome(state.record) : null;
  return createPortal(
    <div className="asset-publish-backdrop" data-testid="asset-publish-backdrop">
      <aside
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="asset-publish-title"
        className="asset-publish-drawer"
        data-testid="asset-publish-drawer"
      >
        <header className="asset-publish-header">
          <h2 id="asset-publish-title" data-testid="asset-publish-title">
            {text('title')}
          </h2>
          <button
            type="button"
            className="asset-publish-close"
            onClick={onClose}
            aria-label={text('close')}
            data-testid="asset-publish-close"
          >
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        <div className="asset-publish-notice" data-testid="asset-publish-notice">
          <Info size={14} aria-hidden="true" />
          <span>{text('review')}</span>
        </div>
        <div className="asset-publish-body" data-testid="asset-publish-body">
          <p className="asset-publish-resource" data-testid="asset-publish-resource">
            {reference.local_id}
          </p>
          <div className="asset-publish-login" data-testid="asset-publish-login">
            <span>{text(state.loggedIn ? 'loginAgain' : 'login')}</span>
            {(['gitcode', 'github'] as const).map((provider) => (
              <button
                type="button"
                key={provider}
                data-testid="asset-publish-provider"
                data-variant={provider}
                onClick={() => login(provider)}
                disabled={state.busy}
              >
                {provider === 'github' ? 'GitHub' : 'GitCode'}
              </button>
            ))}
          </div>
          {state.description?.hub_url && (
            <p data-testid="asset-publish-destination">
              {text('destination')}: {state.description.hub_url}
            </p>
          )}
          {state.loggedIn && state.description?.identity_verified === false && (
            <p data-testid="asset-publish-account-scope">{text('accountScope')}</p>
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
              <fieldset disabled={state.busy || (state.loggedIn && !state.description)}>
                {fields.map((field) => (
                  <label
                    key={field}
                    className="asset-publish-field"
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
                      {fieldHints[field] && (
                        <span
                          className="asset-publish-hint"
                          title={t(`skills.publishForm.${fieldHints[field]}`)}
                          data-testid="asset-publish-field-hint"
                          data-variant={field}
                        >
                          <HelpCircle size={14} aria-hidden="true" />
                        </span>
                      )}
                    </span>
                    {field === 'description' || field === 'version_desc' ? (
                      <textarea
                        data-testid={`asset-publish-${field.replaceAll('_', '-')}`}
                        value={state.metadata[field]}
                        onChange={(event) => state.edit({ [field]: event.target.value })}
                        rows={3}
                      />
                    ) : (
                      <input
                        data-testid={`asset-publish-${field.replaceAll('_', '-')}`}
                        value={field === 'tags' ? state.metadata.tags.join(', ') : state.metadata[field]}
                        onChange={(event) =>
                          state.edit({
                            [field]:
                              field === 'tags'
                                ? event.target.value.split(',').map((value) => value.trim())
                                : event.target.value,
                          })
                        }
                        required={['asset_name', 'display_name', 'version'].includes(field)}
                        placeholder={field === 'version' ? text('versionHint') : undefined}
                      />
                    )}
                  </label>
                ))}
                <label className="asset-publish-field">
                  <span data-testid="asset-publish-visibility-label">{text('visibility')}</span>
                  <select
                    data-testid="asset-publish-visibility"
                    value={state.metadata.visibility}
                    onChange={(event) => state.edit({ visibility: event.target.value as 'public' | 'private' })}
                  >
                    <option value="public">{text('public')}</option>
                    <option value="private">{text('private')}</option>
                  </select>
                </label>
                <label className="asset-publish-field">
                  <span data-testid="asset-publish-target-label">{text('target')}</span>
                  <input
                    data-testid="asset-publish-target"
                    value={state.targetAssetId}
                    onChange={(event) => state.setTargetAssetId(event.target.value)}
                  />
                </label>
                <p className="text-text-muted" data-testid="asset-publish-ownership-note">
                  {text('ownership')}
                </p>
                <label className="asset-publish-checkbox">
                  <input
                    type="checkbox"
                    data-testid="asset-publish-force"
                    checked={state.force}
                    onChange={(event) => state.setForce(event.target.checked)}
                  />
                  {text('force')}
                </label>
              </fieldset>
              {invalid && (
                <p className="text-warn" data-testid="asset-publish-validation">
                  {text('invalid')}
                </p>
              )}
              {state.description && !state.description.can_publish && (
                <p role="alert" data-testid="asset-publish-unavailable">
                  {text('unavailable')}
                </p>
              )}
              {state.description?.errors?.map((issue, index) => (
                <pre
                  className="text-danger"
                  data-testid="asset-publish-description-error"
                  data-variant={index}
                  key={index}
                >
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
              <p>{text('review')}</p>
              {state.draft && (
                <>
                  <p data-testid="asset-publish-package-summary">
                    {state.draft.package_name} · {state.draft.version} · {state.draft.size_bytes} bytes
                  </p>
                  {(['files', 'excluded', 'normalizations', 'dependencies', 'warnings', 'errors'] as const).map(
                    (key) => (
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
                    ),
                  )}
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
            <section aria-live="polite" data-testid="asset-publish-result" data-variant={outcome}>
              <h3>{text(outcome as MessageKey)}</h3>
              <p>{state.record.result?.asset_id}</p>
              {state.record.result?.visibility === null && (
                <p role="status" data-testid="asset-publish-visibility-unconfirmed">
                  {i18n.language.startsWith('zh')
                    ? 'Hub 已返回提交结果，但未确认可见范围。请在 Hub 核对，当前不能据此认定为私有或公开。'
                    : 'Hub returned a submission result without confirming visibility. Check it in Hub; public or private visibility is not yet verified.'}
                </p>
              )}
              <p>{state.record.result?.version || state.record.version}</p>
              {state.record.error && (
                <p className="text-danger">
                  {state.record.error.code}: {state.record.error.message}
                </p>
              )}
              <p data-testid="asset-publish-observed-note">{text('observed')}</p>
              {state.record.updated_at && <p>{dateText(state.record.updated_at)}</p>}
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
            </section>
          )}
          {state.loggedIn && (
            <section data-testid="asset-publish-records">
              <h3>{text('records')}</h3>
              <button
                type="button"
                data-testid="asset-publish-refresh"
                disabled={state.busy}
                onClick={() => void state.refresh()}
              >
                {text('refresh')}
              </button>
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
                  {record.result?.version || record.version} · {text(publishOutcome(record) as MessageKey)}
                </button>
              ))}
            </section>
          )}
        </div>
        <footer className="asset-publish-footer" data-testid="asset-publish-footer">
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
        </footer>
      </aside>
    </div>,
    document.body,
  );
}
export function AssetPublishHost() {
  const [selection, setSelection] = useState<{ reference: AssetReference; metadata?: PublishMetadata } | null>(null);
  useEffect(() => {
    const open = (event: Event) => setSelection({ reference: (event as CustomEvent<AssetReference>).detail });
    const restore = () => {
      try {
        const value = sessionStorage.getItem(PUBLISH_RESTORE_KEY);
        if (!value) return;
        const saved = JSON.parse(value);
        if (
          ['skill', 'agent_template', 'plugin', 'mcp'].includes(saved.reference?.kind) &&
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
