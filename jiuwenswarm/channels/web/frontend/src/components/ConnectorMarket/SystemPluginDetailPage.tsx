import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { RefreshCw, Zap } from 'lucide-react';
import { CloseButton, DetailSection, Dialog, EntityHeader, MarkdownPane, PageCard, PageToolbar, Tabs } from '../ui';
import { MarkdownRenderer } from '../MarkdownRenderer';
import { IconAvatar } from './Buttons';
import { ConfirmDialog } from './ConfirmDialog';
import { DefinitionFilePreview, findDefaultDefinitionFile } from '../AgentManagementPanel/DefinitionFilePreview';
import type { AgentFileContent, DefinitionFileEntry, RequestStatus } from '../../features/agentManagement';
import { systemPluginApi } from '../../services/extensionsV2Api';
import { useSystemPluginStore } from '../../stores/systemPluginStore';
import { localizedText } from '../../types/localized';
import UninstallIcon from '../../assets/agent-management/uninstall.svg?react';
import BackIcon from '../../assets/work-mode/arrow-left.svg?react';
import './ConnectorMarket.css';

const README_DIALOG_TITLE_ID = 'system-plugin-readme-dialog-title';

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
  const busy = useSystemPluginStore((s) => !!s.busyIds[id]);
  const loadDetail = useSystemPluginStore((s) => s.loadDetail);
  const install = useSystemPluginStore((s) => s.install);
  const uninstall = useSystemPluginStore((s) => s.uninstall);
  const reload = useSystemPluginStore((s) => s.reload);
  const [loading, setLoading] = useState(!detail);
  const [failed, setFailed] = useState(false);
  const [confirmUninstall, setConfirmUninstall] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [tab, setTab] = useState<DetailTab>('content');
  const [files, setFiles] = useState<DefinitionFileEntry[]>([]);
  const [filesStatus, setFilesStatus] = useState<RequestStatus>('idle');
  const [filesError, setFilesError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [fileContent, setFileContent] = useState<AgentFileContent | null>(null);
  const [fileStatus, setFileStatus] = useState<RequestStatus>('idle');
  const [fileError, setFileError] = useState<string | null>(null);
  const [readmeOpen, setReadmeOpen] = useState(false);
  const [readmeClipped, setReadmeClipped] = useState(false);
  const readmeRef = useRef<HTMLDivElement>(null);
  const filesRevisionRef = useRef(0);
  const fileRevisionRef = useRef(0);

  const selectFile = useCallback(
    async (path: string) => {
      const revision = ++fileRevisionRef.current;
      setSelectedPath(path);
      setFileStatus('loading');
      setFileError(null);
      try {
        const content = await systemPluginApi.file(id, path);
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
      const tree = await systemPluginApi.files(id);
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

  useEffect(() => {
    filesRevisionRef.current += 1;
    fileRevisionRef.current += 1;
    setTab('content');
    setReadmeOpen(false);
    setReadmeClipped(false);
    setFiles([]);
    setFilesStatus('idle');
    setFilesError(null);
    setSelectedPath(null);
    setFileContent(null);
    setFileStatus('idle');
    setFileError(null);
    requestDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, loadDetail]);

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
  const status = detail.active
    ? t('connectorMarket.systemPlugin.statusActive')
    : detail.installed
      ? t('connectorMarket.systemPlugin.statusInstalled')
      : t('connectorMarket.systemPlugin.statusAvailable');
  const busyBlocked = busy || loading;

  async function runAction(
    action: (packageId: string) => Promise<{ ok: boolean; error?: string }>,
    onDone?: () => void,
  ) {
    setActionError(null);
    const result = await action(id);
    if (result.ok) onDone?.();
    else setActionError(result.error ?? t('connectorMarket.common.loadFailed'));
  }

  async function handleUninstall() {
    setConfirmUninstall(false);
    await runAction(uninstall, onDeleted);
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
            status,
            ...detail.tags.map((tag) => localizedText(tag, i18n.language)),
          ]}
          actions={
            <div className="detail-actions" data-testid="connector-market-system-plugin-detail-actions">
              {!detail.installed && (
                <button
                  type="button"
                  className="detail-action detail-action--install"
                  disabled={busyBlocked}
                  onClick={() => { void runAction(install); }}
                >
                  {t('connectorMarket.systemPlugin.install')}
                </button>
              )}
              {detail.installed && (
                <button
                  type="button"
                  className="detail-action"
                  disabled={busyBlocked}
                  onClick={() => { void runAction(reload); }}
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
            <img
              src={detail.preview}
              alt={title}
              className="mx-auto block max-h-[280px] max-w-full rounded-xl border border-border object-contain"
              data-testid="connector-market-system-plugin-detail-preview-image"
            />
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
              {detail.events.map((event) => (
                <PageCard
                  key={event.name}
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
          message={t('connectorMarket.systemPlugin.uninstallConfirm', { name: title })}
          confirmLabel={t('connectorMarket.systemPlugin.uninstall')}
          onCancel={() => setConfirmUninstall(false)}
          onConfirm={() => { void handleUninstall(); }}
        />
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
