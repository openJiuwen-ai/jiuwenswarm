import { ChevronDown } from 'lucide-react';
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CatalogPage, PAGE_SIZE } from './CatalogPage';
import { AgentEditor } from './AgentEditor';
import { DefinitionDetailPage } from './DefinitionDetailPage';
import { AgentUploadDialog } from './AgentUploadDialog';
import { PendingConnectorModals, usePendingConnectorFlow } from '../ConnectorMarket/usePendingConnectorFlow';
import { useConnectorStore } from '../../stores/connectorStore';
import {
  AgentInstallPendingError,
  createAgentManagementClient,
  extractRpcErrorMessage,
  type AgentCatalogItem,
  type AgentDraft,
  type AgentManagementClient,
  type DefinitionFileEntry,
  type McpOption,
  type RequestStatus,
  agentManagementReducer,
  buildCatalogViewModel,
  findFirstPreviewableFile,
  initialAgentManagementState,
  isPreviewableFile,
  mergeAgentDetailWithCatalog,
} from '../../features/agentManagement';
import './agentManagement.css';
import { equipmentListFilter } from '../../features/equipmentMarketplace';
import { PageHeader, PageToolbarSearch } from '../ui';

type PanelView = 'catalog' | 'mine' | 'detail' | 'create';

type AgentManagementPanelProps = {
  isActive?: boolean;
  onUseAgent?: (id: string) => void;
  onUsePrompt?: (id: string, prompt: string) => void;
  onCreateViaChat?: () => void;
  onViewChange?: (view: PanelView) => void;
};

const EMPTY_DRAFT: AgentDraft = {
  id: '',
  name: '',
  description: '',
  persona: '',
  tagIds: [],
  customTags: [],
  skillRefs: [],
  mcpRefs: [],
  suggestedPrompts: [],
};

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message.trim();
  }
  if (error && typeof error === 'object' && 'payload' in error) {
    const payload = (error as { payload?: unknown }).payload;
    if (payload && typeof payload === 'object') {
      const apiError = (payload as { error?: unknown }).error;
      if (typeof apiError === 'string' && apiError.trim()) {
        return apiError.trim();
      }
    }
  }
  return fallback;
}

function getFriendlyErrorMessage(
  error: unknown,
  fallback: string,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  const message = typeof error === 'string' ? error : getErrorMessage(error, fallback);
  const code =
    error && typeof error === 'object' && 'code' in error ? String((error as { code?: unknown }).code || '') : '';
  if (code === 'agent_detail_empty') return translate('agentManagement.states.detailError');
  const normalizedMessage = message.trim();
  if (code === 'REQUEST_TIMEOUT') return translate('network.requestTimeout');
  if (code === 'WS_NOT_READY') return translate('network.connectionUnavailable');
  if (code === 'WS_DISCONNECTED') return translate('network.connectionClosed');
  if (code === 'REQUEST_ABORTED') return translate('network.requestAborted');
  if (/^agent_template package already exists in (?:local|built_in|resources):/i.test(normalizedMessage)) {
    return translate('agentManagement.states.duplicateName');
  }
  if (/^agent_template package not found:/i.test(normalizedMessage)) {
    return translate('agentManagement.states.agentUnavailable');
  }
  if (
    /^agent_template package (?:wrong package_type|conflict):/i.test(normalizedMessage)
  ) {
    return translate('agentManagement.states.agentDefinitionUnavailable');
  }
  if (/^(?:skill not found:|invalid skill name:|missing or invalid skills$)/i.test(normalizedMessage)) {
    return translate('agentManagement.states.skillUnavailable');
  }
  if (/^(?:mcp .* not found|invalid mcp name:|missing or invalid mcps$)/i.test(normalizedMessage)) {
    return translate('agentManagement.states.mcpUnavailable');
  }
  if (/^(?:invalid quick input:|missing or invalid quickInputs$)/i.test(normalizedMessage)) {
    return translate('agentManagement.states.promptInvalid');
  }
  if (/^(?:invalid tag|missing or invalid tags$)/i.test(normalizedMessage)) {
    return translate('agentManagement.states.tagInvalid');
  }
  const invalidField = /^missing or invalid (name|description|persona)$/i.exec(normalizedMessage)?.[1];
  if (invalidField) return translate(`agentManagement.form.errors.${invalidField}Required`);
  if (/^invalid params$/i.test(normalizedMessage)) {
    return translate('agentManagement.states.formInvalid');
  }
  if (/^file not found:/i.test(normalizedMessage)) {
    return translate('agentManagement.files.fileUnavailable');
  }
  if (/^file too large:/i.test(normalizedMessage)) {
    return translate('agentManagement.files.fileTooLarge');
  }
  if (/^file not previewable:/i.test(normalizedMessage)) {
    return translate('agentManagement.files.notPreviewable');
  }
  const connector = /^connector not connected:\s*(.+)$/i.exec(normalizedMessage)?.[1];
  if (connector) return translate('agentManagement.states.connectorUnavailableNamed', { connector });
  return fallback;
}

function deriveAgentId(name: string): string {
  const slug = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug.length >= 3 ? slug.slice(0, 50) : `agent-${Date.now().toString(36)}`;
}

export function AgentManagementPanel({
  isActive = true,
  onUseAgent,
  onUsePrompt,
  onCreateViaChat,
  onViewChange,
}: AgentManagementPanelProps) {
  const { t } = useTranslation();
  const client = useMemo<AgentManagementClient>(() => createAgentManagementClient(), []);
  const [state, dispatch] = useReducer(agentManagementReducer, initialAgentManagementState);
  const [view, setView] = useState<PanelView>('catalog');
  useEffect(() => {
    onViewChange?.(view);
  }, [view, onViewChange]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<'content' | 'files'>('content');
  const [query, setQuery] = useState('');
  const [mineQuery, setMineQuery] = useState('');
  const [category, setCategory] = useState('');
  const [catalogPage, setCatalogPage] = useState(1);
  const [minePage, setMinePage] = useState(1);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detailOrigin, setDetailOrigin] = useState<'catalog' | 'mine'>('catalog');
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionNotice, setActionNotice] = useState<string | null>(null);
  const [connectorFlowId, setConnectorFlowId] = useState<string | null>(null);
  const [draft, setDraft] = useState<AgentDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const [uploadDialogOpen, setUploadDialogOpen] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [mcpOptions, setMcpOptions] = useState<McpOption[]>([]);
  const [mcpStatus, setMcpStatus] = useState<RequestStatus>('idle');
  const catalogRef = useRef<AgentCatalogItem[]>(state.catalog);
  const catalogRevisionRef = useRef(0);
  const panelMountedRef = useRef(false);
  const panelPrevActiveRef = useRef(false);
  const detailRevisionRef = useRef(0);
  const filesRevisionRef = useRef(0);
  const fileRevisionRef = useRef(0);
  const actionNoticeTimerRef = useRef<number | null>(null);
  const installFlowTargetRef = useRef<string | null>(null);
  const reconnectFlowTargetRef = useRef<string | null>(null);
  const connectorError = useConnectorStore((state) => state.error);
  const clearConnectorError = useConnectorStore((state) => state.clearError);
  const formatActionError = useCallback(
    (error: unknown, fallback: string) => getFriendlyErrorMessage(error, fallback, t),
    [t],
  );

  const catalogView = useMemo(
    () =>
      buildCatalogViewModel(state.catalog, {
        scope: 'catalog',
        category,
        query,
        page: catalogPage,
        pageSize: PAGE_SIZE,
      }),
    [state.catalog, category, query, catalogPage],
  );
  const mineView = useMemo(
    () =>
      buildCatalogViewModel(state.catalog, {
        scope: 'mine',
        category: '',
        query: mineQuery,
        page: minePage,
        pageSize: PAGE_SIZE,
      }),
    [state.catalog, mineQuery, minePage],
  );

  const loadCatalog = useCallback(async () => {
    const revision = ++catalogRevisionRef.current;
    dispatch({ type: 'catalog.loading' });
    try {
      const [marketplaceCatalog, mineCatalog] = await Promise.all([
        client.listCatalog({ filter: equipmentListFilter('agent', 'catalog') }),
        client.listCatalog({ filter: equipmentListFilter('agent', 'mine') }),
      ]);
      const catalog = Array.from(
        new Map([...marketplaceCatalog, ...mineCatalog].map((item) => [item.id, item])).values(),
      );
      if (revision !== catalogRevisionRef.current) return;
      catalogRef.current = catalog;
      dispatch({ type: 'catalog.loaded', catalog });
    } catch (error) {
      if (revision !== catalogRevisionRef.current) return;
      catalogRef.current = [];
      dispatch({ type: 'catalog.error', message: formatActionError(error, t('agentManagement.states.loadError')) });
    }
  }, [client, formatActionError, t]);

  const loadSkills = useCallback(async () => {
    dispatch({ type: 'skills.loading' });
    try {
      const options = await client.listSkillOptions();
      dispatch({ type: 'skills.loaded', options });
    } catch {
      dispatch({ type: 'skills.error' });
    }
  }, [client]);

  const loadMcps = useCallback(async () => {
    setMcpStatus('loading');
    try {
      const options = await client.listMcpOptions();
      setMcpOptions(options);
      setMcpStatus('success');
    } catch {
      setMcpStatus('error');
    }
  }, [client]);

  // 切换到专家页面时刷新目录（面板常驻挂载、切走仅隐藏，聊天里新建的专家
  // 不会主动通知前端），沿用 SkillPanel 的激活转换检测；首次挂载也走此入口，
  // 避免与旧的 mount-only 请求重复。
  useEffect(() => {
    const prevIsActive = panelPrevActiveRef.current;
    const isInitialMount = !panelMountedRef.current;
    panelMountedRef.current = true;
    if (isActive && (!prevIsActive || isInitialMount)) {
      void loadCatalog();
    }
    panelPrevActiveRef.current = isActive;
  }, [isActive, loadCatalog]);

  useEffect(() => {
    return () => {
      if (actionNoticeTimerRef.current !== null) {
        window.clearTimeout(actionNoticeTimerRef.current);
      }
    };
  }, []);

  const openDetail = useCallback(
    async (id: string) => {
      const revision = ++detailRevisionRef.current;
      if (view !== 'detail') {
        setDetailOrigin(view === 'mine' ? 'mine' : 'catalog');
      }
      setActionError(null);
      setSelectedId(id);
      setDetailTab('content');
      setView('detail');
      filesRevisionRef.current += 1;
      fileRevisionRef.current += 1;
      dispatch({ type: 'detail.loading' });
      try {
        const detail = await client.getDefinition(id);
        if (revision !== detailRevisionRef.current) return;
        dispatch({
          type: 'detail.loaded',
          detail: mergeAgentDetailWithCatalog(
            detail,
            catalogRef.current.find((item) => item.id === id),
          ),
        });
      } catch (error) {
        if (revision !== detailRevisionRef.current) return;
        dispatch({ type: 'detail.error', message: formatActionError(error, t('agentManagement.states.detailError')) });
      }
    },
    [client, formatActionError, t, view],
  );

  const loadFiles = useCallback(
    async (id: string): Promise<DefinitionFileEntry[] | null> => {
      const revision = ++filesRevisionRef.current;
      dispatch({ type: 'files.loading' });
      try {
        const files = await client.getDefinitionFiles(id);
        if (revision !== filesRevisionRef.current) return null;
        dispatch({ type: 'files.loaded', files });
        return files;
      } catch (error) {
        if (revision !== filesRevisionRef.current) return null;
        dispatch({ type: 'files.error', message: formatActionError(error, t('agentManagement.files.loadError')) });
        return null;
      }
    },
    [client, formatActionError, t],
  );

  const handleTabChange = (tab: 'content' | 'files') => {
    setDetailTab(tab);
    if (tab === 'files' && selectedId && state.filesStatus === 'idle') {
      void loadFiles(selectedId).then((files) => {
        const firstPreviewableFile = files ? findFirstPreviewableFile(files) : null;
        if (firstPreviewableFile) void handleSelectFile(firstPreviewableFile);
      });
    }
  };

  const handleSelectFile = async (relativePath: string) => {
    const revision = ++fileRevisionRef.current;
    if (!selectedId || !isPreviewableFile(relativePath)) {
      dispatch({ type: 'file.unsupported', relativePath });
      return;
    }
    dispatch({ type: 'file.loading', relativePath });
    try {
      const content = await client.getDefinitionFile(selectedId, relativePath);
      if (revision !== fileRevisionRef.current || content.relativePath !== relativePath) return;
      dispatch({ type: 'file.loaded', content });
    } catch (error) {
      if (revision !== fileRevisionRef.current) return;
      dispatch({ type: 'file.error', message: formatActionError(error, t('agentManagement.files.readError')) });
    }
  };

  const refreshAfterAction = useCallback(
    async (id: string) => {
      await loadCatalog();
      if (selectedId === id && view === 'detail') await openDetail(id);
    },
    [loadCatalog, openDetail, selectedId, view],
  );

  const retryInstallAfterConnect = useCallback(
    async (id: string) => {
      setActionError(null);
      try {
        await client.installDefinition(id);
        await refreshAfterAction(id);
      } catch (error) {
        setActionError(formatActionError(error, t('agentManagement.states.actionError')));
      } finally {
        setBusyId(null);
      }
    },
    [client, refreshAfterAction, t],
  );

  const refreshAfterReconnect = useCallback(
    async (id: string) => {
      setActionError(null);
      try {
        await refreshAfterAction(id);
      } catch (error) {
        setActionError(formatActionError(error, t('agentManagement.states.actionError')));
      } finally {
        setBusyId(null);
      }
    },
    [refreshAfterAction, t],
  );

  const installFlow = usePendingConnectorFlow(() => {
    const id = installFlowTargetRef.current;
    installFlowTargetRef.current = null;
    setConnectorFlowId(null);
    if (id) void retryInstallAfterConnect(id);
  });

  const reconnectFlow = usePendingConnectorFlow(() => {
    const id = reconnectFlowTargetRef.current;
    reconnectFlowTargetRef.current = null;
    setConnectorFlowId(null);
    if (id) void refreshAfterReconnect(id);
  });

  useEffect(() => {
    if (!connectorFlowId) return;
    const flowActive =
      installFlow.active ||
      reconnectFlow.active ||
      Boolean(
        installFlow.tokenTarget || installFlow.authTarget || reconnectFlow.tokenTarget || reconnectFlow.authTarget,
      );
    if (flowActive) return;

    const id = installFlowTargetRef.current || reconnectFlowTargetRef.current;
    if (connectorError) setActionError(formatActionError(connectorError, t('agentManagement.states.actionError')));
    installFlowTargetRef.current = null;
    reconnectFlowTargetRef.current = null;
    setConnectorFlowId(null);
    setBusyId((current) => (current === id ? null : current));
  }, [
    connectorError,
    connectorFlowId,
    formatActionError,
    installFlow.active,
    installFlow.authTarget,
    installFlow.tokenTarget,
    reconnectFlow.active,
    reconnectFlow.authTarget,
    reconnectFlow.tokenTarget,
    t,
  ]);

  const handleInstall = async (id: string) => {
    setBusyId(id);
    setActionError(null);
    setActionNotice(null);
    clearConnectorError();
    try {
      const result = await client.installDefinition(id);
      if (result.kind === 'auth_required') {
        throw new Error(t('agentManagement.states.authRequired'));
      }
      await refreshAfterAction(id);
    } catch (error) {
      if (error instanceof AgentInstallPendingError) {
        installFlowTargetRef.current = id;
        setConnectorFlowId(id);
        installFlow.start(error.pendingConnectors);
        return;
      }
      setActionError(formatActionError(error, t('agentManagement.states.actionError')));
    } finally {
      if (installFlowTargetRef.current !== id) setBusyId(null);
    }
  };

  const handleUninstall = async (id: string) => {
    setBusyId(id);
    setActionError(null);
    setActionNotice(null);
    try {
      const fromDetail = view === 'detail' && selectedId === id;
      const result = await client.uninstallDefinition(id);
      if (fromDetail) {
        await loadCatalog();
        goBackToCatalog();
      } else {
        await refreshAfterAction(id);
      }
      if (result.notice) setActionNotice(result.notice);
    } catch (error) {
      setActionError(formatActionError(error, t('agentManagement.states.actionError')));
    } finally {
      setBusyId(null);
    }
  };

  const handleUse = (id: string) => {
    const item = catalogRef.current.find((candidate) => candidate.id === id);
    if (!item?.installed || item.connectionState !== 'connected' || item.enabled === false) return;
    onUseAgent?.(item.runtimePackageName);
  };

  const handleReconnect = async (id: string) => {
    setBusyId(id);
    setActionError(null);
    setActionNotice(null);
    clearConnectorError();
    try {
      const detail = state.detail?.id === id ? state.detail : await client.getDefinition(id);
      if (detail.pendingConnectors.length === 0) {
        setActionError(t('agentManagement.states.connectionUnavailable'));
        return;
      }
      reconnectFlowTargetRef.current = id;
      setConnectorFlowId(id);
      reconnectFlow.start(detail.pendingConnectors);
    } catch (error) {
      setActionError(formatActionError(error, t('agentManagement.states.actionError')));
    } finally {
      if (reconnectFlowTargetRef.current !== id) setBusyId(null);
    }
  };

  const openCreate = () => {
    setCreateMenuOpen(false);
    setDraft(EMPTY_DRAFT);
    setCreateError(null);
    setActionError(null);
    setActionNotice(null);
    setView('create');
    if (state.skillsStatus === 'idle') void loadSkills();
    void loadMcps();
  };

  const handleCreate = async () => {
    setSaving(true);
    setCreateError(null);
    setActionError(null);
    setActionNotice(null);
    try {
      await client.createAgent({ ...draft, id: draft.id || deriveAgentId(draft.name) });
      await loadCatalog();
      setMineQuery('');
      setMinePage(1);
      setView('mine');
    } catch (error) {
      setCreateError(formatActionError(error, t('agentManagement.form.saveError')));
    } finally {
      setSaving(false);
    }
  };

  const openUpload = () => {
    setCreateMenuOpen(false);
    setActionError(null);
    setActionNotice(null);
    setUploadError(null);
    setUploadDialogOpen(true);
  };

  const handleUpload = async (path: string) => {
    if (actionNoticeTimerRef.current !== null) {
      window.clearTimeout(actionNoticeTimerRef.current);
      actionNoticeTimerRef.current = null;
    }
    setActionError(null);
    setActionNotice(null);
    setUploadError(null);
    try {
      const result = await client.importAgentTemplate(path);
      await loadCatalog();
      setUploadDialogOpen(false);
      setUploadError(null);
      setMineQuery('');
      setMinePage(1);
      setView('mine');
      const notice = t('agentManagement.states.uploadSuccess', { id: result.id });
      setActionNotice(notice);
      actionNoticeTimerRef.current = window.setTimeout(() => {
        setActionNotice((current) => (current === notice ? null : current));
        actionNoticeTimerRef.current = null;
      }, 3000);
    } catch (error) {
      setUploadError(extractRpcErrorMessage(error, t('agentManagement.states.uploadError')));
    }
  };

  const goBackToCatalog = () => {
    setActionError(null);
    setActionNotice(null);
    setView(detailOrigin);
  };

  const pendingConnectorModals = (
    <>
      <PendingConnectorModals flow={installFlow} />
      <PendingConnectorModals flow={reconnectFlow} />
    </>
  );

  const uploadDialog = uploadDialogOpen ? (
    <AgentUploadDialog
      error={uploadError}
      onCancel={() => {
        setUploadDialogOpen(false);
        setUploadError(null);
      }}
      onConfirm={handleUpload}
    />
  ) : null;

  if (view === 'detail') {
    return (
      <div className="app-page-body">
        <main
          className="page-content agent-management-panel agent-management-panel--detail"
          data-source={client.source}
          data-testid="agent-management-panel"
          data-variant="detail"
        >
          <DefinitionDetailPage
            detail={state.detail}
            detailStatus={state.detailStatus}
            detailError={state.detailError}
            detailTab={detailTab}
            files={state.files}
            filesStatus={state.filesStatus}
            filesError={state.filesError}
            selectedFilePath={state.selectedFilePath}
            fileContent={state.fileContent}
            fileStatus={state.fileStatus}
            fileError={state.fileError}
            actionError={actionError}
            actionNotice={actionNotice}
            busy={busyId === selectedId}
            onBack={goBackToCatalog}
            onRetry={() => selectedId && void openDetail(selectedId)}
            onTabChange={handleTabChange}
            onRetryFiles={() =>
              selectedId &&
              (state.detail?.source === 'local' || state.detail?.installed === true) &&
              void loadFiles(selectedId).then((files) => {
                const firstPreviewableFile = files ? findFirstPreviewableFile(files) : null;
                if (firstPreviewableFile) void handleSelectFile(firstPreviewableFile);
              })
            }
            onSelectFile={handleSelectFile}
            onUse={handleUse}
            onUsePrompt={onUsePrompt}
            onReconnect={handleReconnect}
            onInstall={handleInstall}
            onUninstall={handleUninstall}
          />
        </main>
        {pendingConnectorModals}
        {uploadDialog}
      </div>
    );
  }

  if (view === 'create') {
    return (
      <div className="app-page-body">
        <main
          className="page-content agent-management-panel agent-management-panel--create"
          data-source={client.source}
          data-testid="agent-management-panel"
          data-variant="create"
        >
          <AgentEditor
            draft={draft}
            skillOptions={state.skillOptions}
            skillsStatus={state.skillsStatus}
            mcpOptions={mcpOptions}
            mcpStatus={mcpStatus}
            saving={saving}
            error={createError}
            onChange={setDraft}
            onReloadSkills={loadSkills}
            onReloadMcps={loadMcps}
            onCancel={() => {
              setActionError(null);
              setActionNotice(null);
              setView('mine');
            }}
            onSave={handleCreate}
          />
        </main>
        {pendingConnectorModals}
        {uploadDialog}
      </div>
    );
  }

  const isMine = view === 'mine';
  return (
    <div className="app-page-body">
      <main
        className={`page-content agent-management-panel agent-management-panel--${isMine ? 'mine' : 'catalog'}`}
        data-source={client.source}
        data-testid="agent-management-panel"
        data-variant={isMine ? 'mine' : 'catalog'}
      >
        {/* 固定区（header/toolbar/提示）：page-shell 限宽 1400px 居中，与下方滚动列共用内容线 */}
        <div className="page-shell flex-none">
        <PageHeader title={t('agentManagement.title')} subtitle={t('agentManagement.subtitle')} />
        <div className="page-toolbar" data-testid="page-toolbar">
          <nav
            className="chat-picker-panel__tabs"
            role="tablist"
            aria-label={t('agentManagement.tabsLabel')}
            data-testid="agent-management-primary-tabs"
          >
            <button
              type="button"
              role="tab"
              aria-selected={!isMine}
              data-testid="agent-management-primary-tab"
              data-variant="catalog"
              className={!isMine ? 'is-active' : ''}
              onClick={() => {
                setCreateMenuOpen(false);
                setActionError(null);
                setActionNotice(null);
                setView('catalog');
              }}
            >
              {t('agentManagement.tabs.catalog')}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={isMine}
              data-testid="agent-management-primary-tab"
              data-variant="mine"
              className={isMine ? 'is-active' : ''}
              onClick={() => {
                setCreateMenuOpen(false);
                setActionError(null);
                setActionNotice(null);
                setView('mine');
              }}
            >
              {t('agentManagement.tabs.mine')}
            </button>
          </nav>
          <div className="agent-management-primary-actions" data-testid="agent-management-primary-actions">
            <PageToolbarSearch
              wrapperTestId="agent-management-search"
              inputTestId="agent-management-search-input"
              type="search"
              name="agent-management-search"
              aria-label={t('agentManagement.searchLabel')}
              autoComplete="off"
              disabled={connectorFlowId !== null}
              value={isMine ? mineQuery : query}
              onChange={(event) =>
                isMine
                  ? (setMineQuery(event.target.value), setMinePage(1))
                  : (setQuery(event.target.value), setCatalogPage(1))
              }
              placeholder={t(isMine ? 'agentManagement.searchMine' : 'agentManagement.searchCatalog')}
            />
            {isMine ? (
              <div className="agent-management-create-menu" data-testid="agent-management-create-menu">
                <button
                  type="button"
                  className="agent-management-button agent-management-button--primary agent-management-create"
                  aria-haspopup="menu"
                  aria-expanded={createMenuOpen}
                  data-testid="agent-management-create-button"
                  onClick={() => setCreateMenuOpen((open) => !open)}
                >
                  {t('agentManagement.actions.create')}
                  <ChevronDown size={15} aria-hidden="true" />
                </button>
                {createMenuOpen ? (
                  <div
                    className="agent-management-create-menu__popover"
                    role="menu"
                    data-testid="agent-management-create-menu-popover"
                  >
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="agent-management-create-menu-item"
                      data-variant="create-first"
                      onClick={openCreate}
                    >
                      {t('agentManagement.actions.createFirst')}
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="agent-management-create-menu-item"
                      data-variant="create-by-chat"
                      onClick={() => {
                        setCreateMenuOpen(false);
                        onCreateViaChat?.();
                      }}
                    >
                      {t('agentManagement.actions.createByChat')}
                    </button>
                    <button type="button" role="menuitem" onClick={openUpload}>
                      {t('agentManagement.actions.createByUpload')}
                    </button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        </div>
        {actionError ? (
          <div className="agent-management-inline-error" role="alert" data-testid="agent-management-inline-error">
            {actionError}
          </div>
        ) : null}
        {actionNotice ? (
          <div className="agent-management-inline-notice" role="status" data-testid="agent-management-inline-notice">
            {actionNotice}
          </div>
        ) : null}
        </div>
        <CatalogPage
          scope={isMine ? 'mine' : 'catalog'}
          items={isMine ? mineView.items : catalogView.items}
          totalItems={isMine ? mineView.totalItems : catalogView.totalItems}
          page={isMine ? mineView.page : catalogView.page}
          totalPages={isMine ? mineView.totalPages : catalogView.totalPages}
          query={isMine ? mineQuery : query}
          category={category}
          status={state.catalogStatus}
          error={state.catalogError}
          busyId={busyId}
          onCategoryChange={(value) => {
            setCategory(value);
            setCatalogPage(1);
          }}
          onPageChange={(value) => (isMine ? setMinePage(value) : setCatalogPage(value))}
          onRetry={loadCatalog}
          onOpen={openDetail}
          onUse={handleUse}
          onReconnect={handleReconnect}
          onInstall={handleInstall}
          onUninstall={handleUninstall}
          onCreate={openCreate}
        />
        {pendingConnectorModals}
        {uploadDialog}
      </main>
    </div>
  );
}
