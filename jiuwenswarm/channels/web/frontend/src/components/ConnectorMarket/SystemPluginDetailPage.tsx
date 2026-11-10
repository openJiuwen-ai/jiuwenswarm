import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { RefreshCw, Zap } from 'lucide-react';
import { CloseButton, DetailSection, Dialog, EntityHeader, MarkdownPane, PageCard, PageToolbar, Select, Tabs } from '../ui';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { IconAvatar } from './Buttons';
import { ConfirmDialog } from './ConfirmDialog';
import { DefinitionFilePreview, findDefaultDefinitionFile } from '../AgentManagementPanel/DefinitionFilePreview';
import type { AgentFileContent, DefinitionFileEntry, RequestStatus } from '../../features/agentManagement';
import { systemPluginApi } from '../../services/extensionsV2Api';
import { useSystemPluginStore } from '../../stores/systemPluginStore';
import { localizedText } from '../../types/localized';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import PromptSendIcon from '../../assets/agent-management/prompt-send.svg?react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import { Switch } from '../Switch';
import { openAssetPublish } from '../../features/assetPublishEvents';
import './ConnectorMarket.css';

const README_DIALOG_TITLE_ID = 'system-plugin-readme-dialog-title';
const PREVIEW_DIALOG_TITLE_ID = 'system-plugin-preview-dialog-title';

interface SystemPluginDetailPageProps {
  id: string;
  onBack: () => void;
  onDeleted?: () => void;
}

type DetailTab = 'content' | 'files';

function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error);
}

export function SystemPluginDetailPage({ id, onBack, onDeleted }: SystemPluginDetailPageProps) {
  const { t, i18n } = useTranslation();
  const detail = useSystemPluginStore((s) => s.detailCache[id]);
  const packageId = detail?.packageId || id;
  const busy = useSystemPluginStore((s) => !!s.busyIds[id] || !!s.busyIds[packageId]);
  const loadDetail = useSystemPluginStore((s) => s.loadDetail);
  const install = useSystemPluginStore((s) => s.install);
  const uninstall = useSystemPluginStore((s) => s.uninstall);
  const reload = useSystemPluginStore((s) => s.reload);
  const setEnabled = useSystemPluginStore((s) => s.setEnabled);
  const [loading, setLoading] = useState(!detail);
  const [failed, setFailed] = useState(false);
  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [pendingVersion, setPendingVersion] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>('content');
  const [files, setFiles] = useState<DefinitionFileEntry[]>([]);
  const [filesStatus, setFilesStatus] = useState<RequestStatus>('idle');
  const [filesError, setFilesError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<AgentFileContent | null>(null);
  const [fileStatus, setFileStatus] = useState<RequestStatus>('idle');
  const [fileError, setFileError] = useState<string | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [readmeOpen, setReadmeOpen] = useState(false);
  const [readmeClipped, setReadmeClipped] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);
  const [versionLoading, setVersionLoading] = useState(false);
  const readmeRef = useRef<HTMLDivElement>(null);
  const versionRef = useRef<string | undefined>(undefined);
  const filesRevisionRef = useRef(0);
  const fileRevisionRef = useRef(0);

  const selectFile = useCallback(
    async (path: string) => {
      const revision = ++fileRevisionRef.current;
      setSelectedPath(path);
      setFileStatus('loading');
      setFileError(null);
      try {
        const content = await systemPluginApi.file(id, path, versionRef.current);
        if (revision !== fileRevisionRef.current) return;
        setFileContent(content);
        setFileStatus('success');
      } catch (error) {
        if (revision !== fileRevisionRef.current) return;
        setFileContent(null);
        setFileError(messageOf(error));
        setFileStatus('error');
      }
    },
    [id],
  );

  const requestFiles = useCallback(async () => {
    const revision = ++filesRevisionRef.current;
    setFilesStatus('loading');
    setFilesError(null);
    try {
      const tree = await systemPluginApi.files(id, versionRef.current);
      if (revision !== filesRevisionRef.current) return;
      setFiles(tree);
      setFilesStatus('success');
      const first = findDefaultDefinitionFile(tree);
      if (first) await selectFile(first);
    } catch (error) {
      if (revision !== filesRevisionRef.current) return;
      setFilesError(messageOf(error));
      setFilesStatus('error');
    }
  }, [id, selectFile]);

  const requestDetail = () => {
    setLoading(true);
    setFailed(false);
    void loadDetail(id).then((ok) => {
      setLoading(false);
      setFailed(!ok);
    });
  };

  const resetPanes = useCallback(() => {
    filesRevisionRef.current += 1;
    fileRevisionRef.current += 1;
    setFiles([]);
    setFilesStatus('idle');
    setFilesError(null);
    setSelectedPath(null);
    setFileContent(null);
    setFileStatus('idle');
    setFileError(null);
  }, []);

  useEffect(() => {
    setTab('content');
    setReadmeOpen(false);
    setReadmeClipped(false);
    setSelectedVersion(null);
    setPendingVersion(null);
    versionRef.current = undefined;
    resetPanes();
    requestDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, loadDetail, resetPanes]);

  useEffect(() => {
    if (selectedVersion !== null || !detail) return;
    const installed = detail.installedVersion || (detail.installed ? detail.version : '');
    versionRef.current = installed && (detail.versions ?? []).includes(installed)
      ? installed
      : detail.version || undefined;
  }, [detail, selectedVersion]);

  useEffect(() => {
    if (tab === 'files' && filesStatus === 'idle') void requestFiles();
  }, [tab, filesStatus, requestFiles]);

  useEffect(() => {
    const node = readmeRef.current;
    if (!node || readmeOpen) return;
    setReadmeClipped(node.scrollHeight > node.clientHeight + 1);
  }, [id, detail?.readme, i18n.language, readmeOpen]);

  if (!detail) {
    return (
      <div className={`flex min-h-0 flex-1 flex-col${loading ? ' detail-loading-shell' : ''}`} data-testid="connector-market-system-plugin-detail-state">
        <button type="button" onClick={onBack} className="detail-back">
          <BackIcon aria-hidden="true" />
          {t('connectorMarket.common.back')}
        </button>
        <div
          className={loading ? 'detail-loading-center text-text-muted' : 'flex min-h-40 flex-col items-center justify-center gap-3 text-text-muted'}
          data-testid="connector-market-system-plugin-detail-state-body"
        >
          <p role={failed ? 'alert' : undefined}>
            {loading ? t('common.loading') : t('connectorMarket.common.loadFailed')}
          </p>
          {failed && (
            <button
              type="button"
              onClick={requestDetail}
              className="rounded-lg border border-border px-4 py-2 text-[14px] text-text hover:bg-hover"
            >
              {t('common.retry')}
            </button>
          )}
        </div>
      </div>
    );
  }

  const title = localizedText(detail.displayName, i18n.language);
  const description = localizedText(detail.displayDescription, i18n.language);
  const details = localizedText(detail.details, i18n.language);
  const readme = localizedText(detail.readme, i18n.language);
  const status = !detail.installed
    ? t('connectorMarket.systemPlugin.statusAvailable')
    : !detail.enabled
      ? t('connectorMarket.systemPlugin.statusDisabled')
      : detail.active
        ? t('connectorMarket.systemPlugin.statusActive')
        : t('connectorMarket.systemPlugin.statusInstalled');
  const source = detail.source === 'hub'
    ? t('connectorMarket.systemPlugin.sourceHub')
    : detail.source === 'local'
      ? t('connectorMarket.systemPlugin.sourceLocal')
      : '';
  const busyBlocked = busy || loading || versionLoading;
  const versions = detail.versions ?? [];
  const installedVersion = detail.installedVersion || (detail.installed ? detail.version : '');
  const initialVersion = installedVersion && versions.includes(installedVersion)
    ? installedVersion
    : detail.version;
  const currentVersion = selectedVersion && versions.includes(selectedVersion)
    ? selectedVersion
    : initialVersion;
  const uninstallTarget = installedVersion || currentVersion;
  const versionOptions = [...versions]
    .reverse()
    .map((version) => ({ value: version, label: version }));

  async function runAction(
    action: () => Promise<{ ok: boolean; error?: string }>,
    onDone?: () => void,
  ): Promise<boolean> {
    setActionError(null);
    const result = await action();
    if (result.ok) {
      onDone?.();
      return true;
    }
    setActionError(result.error ?? t('connectorMarket.common.loadFailed'));
    return false;
  }

  async function handleInstall(version = currentVersion) {
    return runAction(async () => {
      const result = await install(id, version);
      if (result.ok) await loadDetail(id, version);
      return result;
    });
  }

  async function handleReload() {
    await runAction(async () => {
      const result = await reload(packageId);
      if (result.ok) await loadDetail(id, currentVersion);
      return result;
    });
  }

  async function handleUninstall(version?: string) {
    setConfirmUninstall(false);
    const ok = await runAction(() => uninstall(packageId, version));
    if (!ok) return;
    if (!version) {
      onDeleted?.();
      return;
    }
    setSelectedVersion(null);
    resetPanes();
    if (!(await loadDetail(id))) onDeleted?.();
  }

  async function handleVersionChange(version: string) {
    if (!version || version === currentVersion) return;
    setActionError(null);
    setSelectedVersion(version);
    versionRef.current = version;
    resetPanes();
    setVersionLoading(true);
    const ok = await loadDetail(id, version);
    setVersionLoading(false);
    if (!ok) setActionError(t('connectorMarket.common.loadFailed'));
  }

  function handleVersionSelect(version: string) {
    if (!version || version === currentVersion) return;
    if (detail.installed && installedVersion && version !== installedVersion) {
      setPendingVersion(version);
      return;
    }
    void handleVersionChange(version);
  }

  async function handleSwitchVersion() {
    const version = pendingVersion;
    if (!version) return;
    const previous = versionRef.current || currentVersion;
    setPendingVersion(null);
    setSelectedVersion(version);
    versionRef.current = version;
    resetPanes();
    if (await handleInstall(version)) return;
    setSelectedVersion(previous);
    versionRef.current = previous;
    await loadDetail(id, previous);
  }

  async function handleEnabledChange(enable: boolean) {
    await runAction(async () => {
      const result = await setEnabled(packageId, enable);
      if (result.ok) await loadDetail(id, currentVersion);
      return result;
    });
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="connector-market-system-plugin-detail">
      <button type="button" onClick={onBack} className="detail-back">
        <BackIcon aria-hidden="true" />
        {t('connectorMarket.common.back')}
      </button>

      <div className="detail-body relative flex-1 min-h-0 overflow-y-auto pb-6">
        <EntityHeader
          testId="connector-market-system-plugin-detail-header"
          avatar={{ name: title, iconUrl: detail.avatar ?? undefined, testId: 'connector-market-system-plugin-detail-avatar' }}
          title={title}
          titleTestId="connector-market-system-plugin-detail-name"
          tags={[
            {
              label: t('connectorMarket.systemPlugin.badge'),
              testId: 'connector-market-system-plugin-detail-integration-type',
            },
            ...(source
              ? [{ label: source, testId: 'connector-market-system-plugin-detail-source' }]
              : []),
            { label: status, testId: 'connector-market-system-plugin-detail-status' },
            ...detail.tags.map((tag) => localizedText(tag, i18n.language)),
          ]}
          actions={
            <div className="detail-actions" data-testid="connector-market-system-plugin-detail-actions">
              {versions.length > 0 && (
                <Select
                  value={currentVersion}
                  options={versionOptions}
                  onChange={handleVersionSelect}
                  disabled={busyBlocked}
                  aria-label={t('connectorMarket.systemPlugin.version')}
                  title={t('connectorMarket.systemPlugin.version')}
                  className="h-7 min-w-[112px] text-[13px]"
                  data-testid="connector-market-system-plugin-detail-versions"
                />
              )}
              {!detail.installed && (
                <button
                  type="button"
                  className="detail-action detail-action--install"
                  disabled={busyBlocked}
                  onClick={() => { void handleInstall(); }}
                >
                  {t('connectorMarket.systemPlugin.install')}
                </button>
              )}
              {installedVersion && (
                <span className="detail-action detail-action--installed" data-testid="connector-market-system-plugin-detail-installed">
                  {t('connectorMarket.systemPlugin.installedVersion', { version: installedVersion })}
                </span>
              )}
              {detail.installed && (
                <button
                  type="button"
                  className="detail-action"
                  disabled={busyBlocked}
                  onClick={() => { void handleReload(); }}
                >
                  <RefreshCw size={14} />
                  {t('connectorMarket.systemPlugin.reload')}
                </button>
              )}
              {detail.installed && (
                <button
                  type="button"
                  className="detail-action"
                  disabled={busyBlocked}
                  onClick={() => setConfirmUninstall(true)}
                >
                  <UninstallIcon aria-hidden="true" />
                  {t('connectorMarket.systemPlugin.uninstall')}
                </button>
              )}
              {detail.installed && detail.origin === 'user' && (
                <button
                  type="button"
                  className="detail-action"
                  disabled={busyBlocked}
                  onClick={() => openAssetPublish({ kind: 'system_plugin', local_id: packageId })}
                  data-testid="connector-market-system-plugin-detail-publish"
                >
                  <PromptSendIcon aria-hidden="true" />
                  {t('connectorMarket.systemPlugin.publish')}
                </button>
              )}
              {detail.installed && (
                <span
                  data-testid="connector-market-system-plugin-detail-enabled"
                  title={t(detail.enabled ? 'connectorMarket.systemPlugin.disable' : 'connectorMarket.systemPlugin.enable')}
                >
                  <Switch
                    checked={detail.enabled}
                    disabled={busyBlocked}
                    onChange={(enable) => { void handleEnabledChange(enable); }}
                  />
                </span>
              )}
            </div>
          }
        />

        {actionError && (
          <p
            role="alert"
            className="mb-4 text-[13px] text-danger"
            data-testid="connector-market-system-plugin-detail-error"
          >
            {actionError}
          </p>
        )}

        {detail.preview && (
          <DetailSection
            testId="connector-market-system-plugin-detail-preview"
            titleTestId="connector-market-system-plugin-detail-preview-title"
            title={t('connectorMarket.systemPlugin.preview')}
          >
            <button
              type="button"
              className="group block w-full cursor-zoom-in rounded-xl border border-border p-2 transition-colors hover:border-accent"
              onClick={() => setPreviewOpen(true)}
              aria-label={t('connectorMarket.systemPlugin.previewHint')}
              title={t('connectorMarket.systemPlugin.previewHint')}
              data-testid="connector-market-system-plugin-detail-preview-open"
            >
              <img
                src={detail.preview}
                alt={title}
                className="mx-auto block max-h-[320px] max-w-full rounded-lg object-contain transition-transform duration-200 group-hover:scale-[1.01]"
                data-testid="connector-market-system-plugin-detail-preview-image"
              />
            </button>
          </DetailSection>
        )}

        <DetailSection
          testId="connector-market-system-plugin-detail-ability"
          titleTestId="connector-market-system-plugin-detail-ability-title"
          title={t('connectorMarket.systemPlugin.ability')}
        >
          <p>{description || '—'}</p>
        </DetailSection>

        {readme && (
          <DetailSection
            testId="connector-market-system-plugin-detail-mechanism"
            titleTestId="connector-market-system-plugin-detail-mechanism-title"
            title={t('connectorMarket.systemPlugin.howItWorks')}
          >
            <div className="overflow-hidden rounded-xl border border-border bg-card">
              <div className="relative">
                <div ref={readmeRef} className="max-h-[480px] overflow-hidden p-4">
                  <MarkdownRenderer
                    content={readme}
                    className="chat-text chat-markdown"
                    testId="connector-market-system-plugin-detail-readme"
                  />
                </div>
                {readmeClipped && (
                  <div
                    className="pointer-events-none absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-card to-transparent"
                    aria-hidden="true"
                  />
                )}
              </div>
              {readmeClipped && (
                <div className="flex justify-center border-t border-border px-4 py-2">
                  <button
                    type="button"
                    className="rounded-lg px-3 py-1 text-[13px] text-text-muted hover:bg-hover hover:text-text"
                    onClick={() => setReadmeOpen(true)}
                  >
                    {t('connectorMarket.systemPlugin.expandReadme')}
                  </button>
                </div>
              )}
            </div>
          </DetailSection>
        )}

        {detail.events.length > 0 && (
          <DetailSection
            testId="connector-market-system-plugin-detail-events"
            titleTestId="connector-market-system-plugin-detail-events-title"
            title={t('connectorMarket.systemPlugin.events')}
          >
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
              {detail.events.map((event, index) => (
                <PageCard
                  key={`${event.name}-${index}`}
                  avatar={<IconAvatar icon={<Zap size={21} />} />}
                  title={event.name}
                  description={localizedText(event.description, i18n.language) || '—'}
                  testId="connector-market-system-plugin-detail-event"
                />
              ))}
            </div>
          </DetailSection>
        )}

        <div data-testid="connector-market-system-plugin-detail-tabs-section" className="flex flex-col min-h-0">
          <PageToolbar style={{ marginTop: 0, flexShrink: 0 }}>
            <Tabs
              role="tablist"
              ariaLabel={t('connectorMarket.systemPlugin.tabsLabel')}
              wrapperTestId="connector-market-system-plugin-detail-tabs"
              itemTestId="connector-market-system-plugin-detail-tab"
              className="page-tabs"
              value={tab}
              onChange={setTab}
              items={[
                { value: 'content', label: t('connectorMarket.systemPlugin.contentTab') },
                { value: 'files', label: t('connectorMarket.systemPlugin.filesTab') },
              ]}
            />
          </PageToolbar>
          {tab === 'content' ? (
            <MarkdownPane
              testId="connector-market-system-plugin-detail-content"
              content={details || null}
              emptyText={t('connectorMarket.systemPlugin.noDetails')}
            />
          ) : (
            <DefinitionFilePreview
              files={files}
              filesStatus={filesStatus}
              filesError={filesError}
              selectedFilePath={selectedPath}
              fileContent={fileContent}
              fileStatus={fileStatus}
              fileError={fileError}
              onRetryFiles={() => { void requestFiles(); }}
              onSelectFile={(path) => { void selectFile(path); }}
            />
          )}
        </div>
      </div>

      {confirmUninstall && (
        <ConfirmDialog
          title={t('connectorMarket.systemPlugin.uninstall')}
          message={t('connectorMarket.systemPlugin.uninstallConfirm', {
            name: title,
            version: uninstallTarget,
          })}
          confirmLabel={t('connectorMarket.systemPlugin.uninstallAll')}
          secondaryLabel={t('connectorMarket.systemPlugin.uninstallVersion', {
            version: uninstallTarget,
          })}
          onSecondary={() => { void handleUninstall(uninstallTarget); }}
          onCancel={() => setConfirmUninstall(false)}
          onConfirm={() => { void handleUninstall(); }}
        />
      )}

      {pendingVersion && (
        <ConfirmDialog
          title={t('connectorMarket.systemPlugin.switchVersion')}
          message={t('connectorMarket.systemPlugin.switchVersionConfirm', {
            name: title,
            installed: installedVersion,
            target: pendingVersion,
          })}
          confirmLabel={t('connectorMarket.systemPlugin.switchVersion')}
          onCancel={() => setPendingVersion(null)}
          onConfirm={() => { void handleSwitchVersion(); }}
        />
      )}

      {previewOpen && detail.preview && (
        <Dialog
          open
          titleId={PREVIEW_DIALOG_TITLE_ID}
          className="system-plugin-preview-dialog"
          onCancel={() => setPreviewOpen(false)}
          onBackdropClick={() => setPreviewOpen(false)}
        >
          <div className="flex max-h-[85vh] flex-col">
            <div className="flex flex-none items-center justify-between gap-4 border-b border-border px-5 py-3">
              <h2 id={PREVIEW_DIALOG_TITLE_ID} className="text-base font-semibold text-text">
                {title}
              </h2>
              <CloseButton
                onClick={() => setPreviewOpen(false)}
                ariaLabel={t('common.close')}
                title={t('common.close')}
                testId="connector-market-system-plugin-preview-close"
              />
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-4">
              <img
                src={detail.preview}
                alt={title}
                className="mx-auto block max-h-[70vh] max-w-full rounded-lg object-contain"
                data-testid="connector-market-system-plugin-detail-preview-large"
              />
            </div>
          </div>
        </Dialog>
      )}

      {readmeOpen && readme && (
        <Dialog
          open
          titleId={README_DIALOG_TITLE_ID}
          className="system-plugin-readme-dialog"
          onCancel={() => setReadmeOpen(false)}
          onBackdropClick={() => setReadmeOpen(false)}
        >
          <div className="flex max-h-[70vh] flex-col">
            <div className="flex flex-none items-center justify-between gap-4 border-b border-border px-5 py-3">
              <h2 id={README_DIALOG_TITLE_ID} className="text-base font-semibold text-text">
                {t('connectorMarket.systemPlugin.howItWorks')}
              </h2>
              <CloseButton
                onClick={() => setReadmeOpen(false)}
                ariaLabel={t('common.close')}
                title={t('common.close')}
                testId="connector-market-system-plugin-readme-close"
              />
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
              <MarkdownRenderer
                content={readme}
                className="chat-text chat-markdown"
                testId="connector-market-system-plugin-detail-readme-full"
              />
            </div>
          </div>
        </Dialog>
      )}
    </div>
  );
}
