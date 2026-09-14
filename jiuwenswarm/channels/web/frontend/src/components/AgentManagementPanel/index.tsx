import { ChevronDown } from 'lucide-react';
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { CatalogPage } from './CatalogPage';
import { AgentEditor } from './AgentEditor';
import { DefinitionDetailPage } from './DefinitionDetailPage';
import { AgentGroupEditor } from './AgentGroupEditor';
import { AgentGroupDetailPage } from './AgentGroupDetailPage';
import { DefinitionUploadDialog } from './AgentGroupUploadDialog';
import { GroupCatalogPage, GROUP_PAGE_SIZE } from './GroupCatalogPage';
import { PendingConnectorModals, usePendingConnectorFlow } from '../ConnectorMarket/usePendingConnectorFlow';
import { useConnectorStore } from '../../stores/connectorStore';
import {
  AgentInstallPendingError,
  createAgentGroupManagementClient,
  createAgentManagementClient,
  type AgentCatalogItem,
  type AgentDraft,
  type AgentGroupCatalogItem,
  type AgentGroupDetail,
  type AgentGroupDraft,
  type AgentGroupManagementClient,
  type AgentManagementClient,
  type DefinitionFileEntry,
  type McpOption,
  type RequestStatus,
  agentManagementReducer,
  buildCatalogViewModel,
  buildGroupCatalogViewModel,
  findFirstPreviewableFile,
  initialAgentManagementState,
  isPreviewableFile,
  mergeAgentDetailWithCatalog,
  mergeAgentGroupDetailWithCatalog,
} from '../../features/agentManagement';
import './agentManagement.css';
import { equipmentListFilter } from '../../features/equipmentMarketplace';
import { PageHeader, PageToolbarSearch, Tabs } from '../ui';

type PanelView = 'catalog' | 'teams' | 'mine' | 'detail' | 'group-detail' | 'create' | 'group-create';

type AgentManagementPanelProps = {
  isActive?: boolean;
  onUseAgent?: (id: string) => void;
  onUsePrompt?: (id: string, prompt: string) => void;
  onCreateViaChat?: () => void;
  onUseAgentGroup?: (id: string) => void;
  onUseGroupPrompt?: (id: string, prompt: string) => void;
  onCreateGroupViaChat?: () => void;
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

const EMPTY_GROUP_DRAFT: AgentGroupDraft = {
  id: '',
  name: '',
  description: '',
  persona: '',
  category: '',
  tagIds: [],
  customTags: [],
  leaderId: '',
  memberIds: [],
  skillRefs: [],
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
  if (code === 'AGENT_GROUP_DUPLICATE') return translate('agentManagement.group.states.duplicateName');
  if (code === 'AGENT_GROUP_MEMBER_NOT_FOUND' || code === 'AGENT_GROUP_TEMPLATE_INVALID' || code === 'AGENT_GROUP_MEMBER_INCOMPATIBLE' || code === 'AGENT_GROUP_MEMBER_AGENT_MD') {
    return translate('agentManagement.group.states.memberUnavailable');
  }
  if (code === 'AGENT_GROUP_LEADER_REQUIRED' || code === 'AGENT_GROUP_MEMBERS_REQUIRED' || code === 'AGENT_GROUP_MEMBER_INVALID' || code === 'AGENT_GROUP_LEADER_MEMBER_CONFLICT' || code === 'AGENT_GROUP_MEMBER_RESERVED' || code === 'AGENT_GROUP_MEMBER_DUPLICATE') {
    return translate('agentManagement.group.states.membersInvalid');
  }
  if (code === 'AGENT_GROUP_NAME_INVALID') return translate('agentManagement.states.formInvalid');
  if (/^agent_template package already exists in (?:local|built_in|resources):/i.test(normalizedMessage)) {
    return translate('agentManagement.states.duplicateName');
  }
  if (/^agent_group package already exists in (?:local|built_in|resources):/i.test(normalizedMessage)) {
    return translate('agentManagement.group.states.duplicateName');
  }
  if (/^agent_group package not found:/i.test(normalizedMessage)) {
    return translate('agentManagement.group.states.unavailable');
  }
  if (/^agent_group package (?:missing\/corrupt manifest\.json|wrong package_type|conflict):/i.test(normalizedMessage)) {
    return translate('agentManagement.group.states.definitionUnavailable');
  }
  if (/^(?:missing or invalid leaderId|missing or invalid memberIds|invalid memberId|leaderId must not appear|duplicate memberId|memberId 'leader')/i.test(normalizedMessage)) {
    return translate('agentManagement.group.states.membersInvalid');
  }
  if (/^(?:leader|member) agent_template (?:not found|is invalid|is Team-compatible)/i.test(normalizedMessage)) {
    return translate('agentManagement.group.states.memberUnavailable');
  }
  if (/^agent_template package not found:/i.test(normalizedMessage)) {
    return translate('agentManagement.states.agentUnavailable');
  }
  if (/^agent_template package (?:wrong package_type|conflict):/i.test(normalizedMessage)) {
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

function deriveAgentGroupId(name: string): string {
  const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
  return slug.length >= 3 ? slug.slice(0, 50) : `agent-group-${Date.now().toString(36)}`;
}

export function AgentManagementPanel({
  isActive = true,
  onUseAgent,
  onUsePrompt,
  onCreateViaChat,
  onUseAgentGroup,
  onUseGroupPrompt,
  onCreateGroupViaChat,
  onViewChange,
}: AgentManagementPanelProps) {
  const { t } = useTranslation();
  const client = useMemo<AgentManagementClient>(() => createAgentManagementClient(), []);
  const groupClient = useMemo<AgentGroupManagementClient>(() => createAgentGroupManagementClient(), []);
  const [state, dispatch] = useReducer(agentManagementReducer, initialAgentManagementState);
  const [view, setView] = useState<PanelView>('catalog');
  useEffect(() => {
    onViewChange?.(view);
  }, [view, onViewChange]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [groupSelectedId, setGroupSelectedId] = useState<string | null>(null);
  const [mineKind, setMineKind] = useState<'agent' | 'group'>('agent');
  const [detailTab, setDetailTab] = useState<'content' | 'files'>('content');
  const [query, setQuery] = useState('');
  const [mineQuery, setMineQuery] = useState('');
  const [category, setCategory] = useState('');
  const [groupCategory, setGroupCategory] = useState('');
  const [groupCatalogQuery, setGroupCatalogQuery] = useState('');
  const [groupMineQuery, setGroupMineQuery] = useState('');
  const [groupCatalogPage, setGroupCatalogPage] = useState(1);
  const [groupMinePage, setGroupMinePage] = useState(1);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detailOrigin, setDetailOrigin] = useState<'catalog' | 'mine'>('catalog');
  const [groupDetailOrigin, setGroupDetailOrigin] = useState<'teams' | 'mine'>('teams');
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
  const [groupCatalog, setGroupCatalog] = useState<AgentGroupCatalogItem[]>([]);
  const [groupCatalogStatus, setGroupCatalogStatus] = useState<RequestStatus>('idle');
  const [groupCatalogError, setGroupCatalogError] = useState<string | null>(null);
  const [groupMine, setGroupMine] = useState<AgentGroupCatalogItem[]>([]);
  const [groupMineStatus, setGroupMineStatus] = useState<RequestStatus>('idle');
  const [groupMineError, setGroupMineError] = useState<string | null>(null);
  const [groupDetail, setGroupDetail] = useState<AgentGroupDetail | null>(null);
  const [groupDetailStatus, setGroupDetailStatus] = useState<RequestStatus>('idle');
  const [groupDetailError, setGroupDetailError] = useState<string | null>(null);
  const [groupDetailTab, setGroupDetailTab] = useState<'content' | 'files'>('content');
  const [groupFiles, setGroupFiles] = useState<DefinitionFileEntry[]>([]);
  const [groupFilesStatus, setGroupFilesStatus] = useState<RequestStatus>('idle');
  const [groupFilesError, setGroupFilesError] = useState<string | null>(null);
  const [groupSelectedFilePath, setGroupSelectedFilePath] = useState<string | null>(null);
  const [groupFileContent, setGroupFileContent] = useState<{ relativePath: string; content: string } | null>(null);
  const [groupFileStatus, setGroupFileStatus] = useState<RequestStatus>('idle');
  const [groupFileError, setGroupFileError] = useState<string | null>(null);
  const [groupDraft, setGroupDraft] = useState<AgentGroupDraft>(EMPTY_GROUP_DRAFT);
  const [groupSaving, setGroupSaving] = useState(false);
  const [groupCreateError, setGroupCreateError] = useState<string | null>(null);
  const [groupUploadDialogOpen, setGroupUploadDialogOpen] = useState(false);
  const [groupUploadError, setGroupUploadError] = useState<string | null>(null);
  const catalogRevisionRef = useRef(0);
  const panelMountedRef = useRef(false);
  const panelPrevActiveRef = useRef(false);
  const detailRevisionRef = useRef(0);
  const filesRevisionRef = useRef(0);
  const fileRevisionRef = useRef(0);
  const groupCatalogRef = useRef<AgentGroupCatalogItem[]>([]);
  const groupMineRef = useRef<AgentGroupCatalogItem[]>([]);
  const groupCatalogRevisionRef = useRef(0);
  const groupMineRevisionRef = useRef(0);
  const groupDetailRevisionRef = useRef(0);
  const groupFilesRevisionRef = useRef(0);
  const groupFileRevisionRef = useRef(0);
  const actionNoticeTimerRef = useRef<number | null>(null);
  const installFlowTargetRef = useRef<string | null>(null);
  const reconnectFlowTargetRef = useRef<string | null>(null);
  const connectorError = useConnectorStore((state) => state.error);
  const clearConnectorError = useConnectorStore((state) => state.clearError);
  const formatActionError = useCallback(
    (error: unknown, fallback: string) => getFriendlyErrorMessage(error, fallback, t),
    [t],
  );
  const showActionNotice = useCallback((notice: string) => {
    if (actionNoticeTimerRef.current !== null) {
      window.clearTimeout(actionNoticeTimerRef.current);
    }
    setActionNotice(notice);
    actionNoticeTimerRef.current = window.setTimeout(() => {
      setActionNotice(current => (current === notice ? null : current));
      actionNoticeTimerRef.current = null;
    }, 3000);
  }, []);

  const catalogView = useMemo(
    () =>
      buildCatalogViewModel(state.catalog, {
        scope: 'catalog',
        category,
        query,
      }),
    [state.catalog, category, query],
  );
  const mineView = useMemo(
    () =>
      buildCatalogViewModel(state.catalog, {
        scope: 'mine',
        category: '',
        query: mineQuery,
      }),
    [state.catalog, mineQuery],
  );
  const groupCatalogView = useMemo(
    () => buildGroupCatalogViewModel(groupCatalog, {
      scope: 'catalog',
      category: groupCategory,
      query: groupCatalogQuery,
      page: groupCatalogPage,
      pageSize: GROUP_PAGE_SIZE,
    }),
    [groupCatalog, groupCategory, groupCatalogQuery, groupCatalogPage],
  );
  const groupMineView = useMemo(
    () => buildGroupCatalogViewModel(groupMine, {
      scope: 'mine',
      category: '',
      query: groupMineQuery,
      page: groupMinePage,
      pageSize: GROUP_PAGE_SIZE,
    }),
    [groupMine, groupMineQuery, groupMinePage],
  );

  const loadCatalog = useCallback(async (options: { includeTeamCompatibility?: boolean } = {}) => {
    const revision = ++catalogRevisionRef.current;
    dispatch({ type: 'catalog.loading' });
    try {
      const compatibilityOptions = options.includeTeamCompatibility
        ? { includeTeamCompatibility: true }
        : {};
      const [marketplaceCatalog, mineCatalog] = await Promise.all([
        client.listCatalog({ filter: equipmentListFilter('agent', 'catalog'), ...compatibilityOptions }),
        client.listCatalog({ filter: equipmentListFilter('agent', 'mine'), ...compatibilityOptions }),
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

  const loadGroups = useCallback(async (scope: 'catalog' | 'mine') => {
    const revisionRef = scope === 'catalog' ? groupCatalogRevisionRef : groupMineRevisionRef;
    const setStatus = scope === 'catalog' ? setGroupCatalogStatus : setGroupMineStatus;
    const setError = scope === 'catalog' ? setGroupCatalogError : setGroupMineError;
    const setItems = scope === 'catalog' ? setGroupCatalog : setGroupMine;
    const listRef = scope === 'catalog' ? groupCatalogRef : groupMineRef;
    const revision = ++revisionRef.current;
    setStatus('loading');
    setError(null);
    try {
      const groups = await groupClient.listGroups({ filter: scope === 'catalog' ? 'builtin' : 'local' });
      if (revision !== revisionRef.current) return;
      listRef.current = groups;
      setItems(groups);
      setStatus('success');
    } catch (error) {
      if (revision !== revisionRef.current) return;
      setStatus('error');
      setError(formatActionError(error, t('agentManagement.group.states.loadError')));
    }
  }, [formatActionError, groupClient, t]);

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
    if (view === 'teams') void loadGroups('catalog');
    if (view === 'mine' && mineKind === 'group') void loadGroups('mine');
  }, [loadGroups, mineKind, view]);

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
      setActionNotice(null);
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

  const openGroupDetail = useCallback(
    async (id: string) => {
      const revision = ++groupDetailRevisionRef.current;
      setGroupDetailOrigin(view === 'mine' ? 'mine' : 'teams');
      setGroupSelectedId(id);
      setGroupDetailTab('content');
      setView('group-detail');
      setActionNotice(null);
      if (actionNoticeTimerRef.current !== null) {
        window.clearTimeout(actionNoticeTimerRef.current);
        actionNoticeTimerRef.current = null;
      }
      groupFilesRevisionRef.current += 1;
      groupFileRevisionRef.current += 1;
      setGroupDetail(null);
      setGroupDetailStatus('loading');
      setGroupDetailError(null);
      setGroupFiles([]);
      setGroupFilesStatus('idle');
      setGroupFilesError(null);
      setGroupSelectedFilePath(null);
      setGroupFileContent(null);
      setGroupFileStatus('idle');
      setGroupFileError(null);
      try {
        const detail = await groupClient.getGroup(id);
        if (revision !== groupDetailRevisionRef.current) return;
        const catalogItem = [...groupCatalogRef.current, ...groupMineRef.current].find(item => item.id === id);
        setGroupDetail(mergeAgentGroupDetailWithCatalog(detail, catalogItem));
        setGroupDetailStatus('success');
      } catch (error) {
        if (revision !== groupDetailRevisionRef.current) return;
        setGroupDetailStatus('error');
        setGroupDetailError(formatActionError(error, t('agentManagement.group.states.detailError')));
      }
    },
    [formatActionError, groupClient, t, view],
  );

  const loadGroupFiles = useCallback(
    async (id: string): Promise<DefinitionFileEntry[] | null> => {
      const revision = ++groupFilesRevisionRef.current;
      setGroupFilesStatus('loading');
      setGroupFilesError(null);
      setGroupFileContent(null);
      setGroupFileStatus('idle');
      try {
        const files = await groupClient.getGroupFiles(id);
        if (revision !== groupFilesRevisionRef.current) return null;
        setGroupFiles(files);
        setGroupFilesStatus('success');
        return files;
      } catch (error) {
        if (revision !== groupFilesRevisionRef.current) return null;
        setGroupFilesStatus('error');
        setGroupFilesError(formatActionError(error, t('agentManagement.group.files.loadError')));
        return null;
      }
    },
    [formatActionError, groupClient, t],
  );

  const handleGroupTabChange = (tab: 'content' | 'files') => {
    setGroupDetailTab(tab);
    if (tab === 'files' && groupSelectedId && groupFilesStatus === 'idle') {
      void loadGroupFiles(groupSelectedId).then(files => {
        const firstPreviewableFile = files ? findFirstPreviewableFile(files) : null;
        if (firstPreviewableFile) void handleSelectGroupFile(firstPreviewableFile);
      });
    }
  };

  const handleSelectGroupFile = async (relativePath: string) => {
    const revision = ++groupFileRevisionRef.current;
    if (!groupSelectedId || !isPreviewableFile(relativePath)) {
      setGroupSelectedFilePath(relativePath);
      setGroupFileContent(null);
      setGroupFileStatus('success');
      return;
    }
    setGroupSelectedFilePath(relativePath);
    setGroupFileContent(null);
    setGroupFileStatus('loading');
    setGroupFileError(null);
    try {
      const content = await groupClient.getGroupFile(groupSelectedId, relativePath);
      if (revision !== groupFileRevisionRef.current || content.relativePath !== relativePath) return;
      setGroupFileContent(content);
      setGroupFileStatus('success');
    } catch (error) {
      if (revision !== groupFileRevisionRef.current) return;
      setGroupFileStatus('error');
      setGroupFileError(formatActionError(error, t('agentManagement.group.files.readError')));
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

  const refreshAfterGroupAction = useCallback(
    async (id: string) => {
      await Promise.all([loadGroups('catalog'), loadGroups('mine')]);
      if (groupSelectedId === id && view === 'group-detail') await openGroupDetail(id);
    },
    [groupSelectedId, loadGroups, openGroupDetail, view],
  );

  const handleUseGroup = (id: string) => {
    const item = [...groupCatalogRef.current, ...groupMineRef.current].find(candidate => candidate.id === id);
    if (!item?.installed || !item.capabilities.canUse) return;
    onUseAgentGroup?.(id);
  };

  const handleUseGroupPrompt = (id: string, prompt: string) => {
    const item = [...groupCatalogRef.current, ...groupMineRef.current].find(candidate => candidate.id === id);
    if (!item?.installed || !item.capabilities.canUse) return;
    onUseGroupPrompt?.(id, prompt);
  };

  const handleInstallGroup = async (id: string) => {
    setBusyId(id);
    setActionError(null);
    setActionNotice(null);
    try {
      await groupClient.installGroup(id);
      await refreshAfterGroupAction(id);
    } catch (error) {
      setActionError(formatActionError(error, t('agentManagement.group.states.actionError')));
    } finally {
      setBusyId(null);
    }
  };

  const handleUninstallGroup = async (id: string) => {
    setBusyId(id);
    setActionError(null);
    setActionNotice(null);
    try {
      const fromDetail = view === 'group-detail' && groupSelectedId === id;
      const before = groupDetail?.id === id ? groupDetail : [...groupCatalogRef.current, ...groupMineRef.current].find(item => item.id === id);
      const result = await groupClient.uninstallGroup(id);
      await Promise.all([loadGroups('catalog'), loadGroups('mine')]);
      if (fromDetail && before?.source === 'local') {
        setView('mine');
        setMineKind('group');
      } else if (fromDetail) {
        await openGroupDetail(id);
      }
      if (result.notice) setActionNotice(result.notice);
    } catch (error) {
      setActionError(formatActionError(error, t('agentManagement.group.states.actionError')));
    } finally {
      setBusyId(null);
    }
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
    setMineKind('agent');
    setDraft(EMPTY_DRAFT);
    setCreateError(null);
    setActionError(null);
    setActionNotice(null);
    setView('create');
    if (state.skillsStatus === 'idle') void loadSkills();
    void loadMcps();
  };

  const openGroupCreate = () => {
    setCreateMenuOpen(false);
    setMineKind('group');
    setGroupDraft(EMPTY_GROUP_DRAFT);
    setGroupCreateError(null);
    setActionError(null);
    setActionNotice(null);
    setView('group-create');
    void loadCatalog({ includeTeamCompatibility: true });
    if (state.skillsStatus === 'idle') void loadSkills();
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
      setView('mine');
    } catch (error) {
      setCreateError(formatActionError(error, t('agentManagement.form.saveError')));
    } finally {
      setSaving(false);
    }
  };

  const handleGroupCreate = async () => {
    setGroupSaving(true);
    setGroupCreateError(null);
    setActionError(null);
      setActionNotice(null);
      try {
        const result = await groupClient.createGroup({ ...groupDraft, id: groupDraft.id || deriveAgentGroupId(groupDraft.name) });
        await loadGroups('mine');
      setGroupMineQuery('');
      setGroupMinePage(1);
      setMineKind('group');
      setView('mine');
      showActionNotice(t('agentManagement.group.states.createSuccess', { id: result.id }));
    } catch (error) {
      setGroupCreateError(formatActionError(error, t('agentManagement.group.form.saveError')));
    } finally {
      setGroupSaving(false);
    }
  };

  const openUpload = () => {
    setCreateMenuOpen(false);
    setActionError(null);
    setActionNotice(null);
    setUploadError(null);
    setUploadDialogOpen(true);
  };

  const openGroupUpload = () => {
    setCreateMenuOpen(false);
    setActionError(null);
    setActionNotice(null);
    setGroupUploadError(null);
    setGroupUploadDialogOpen(true);
  };

  const handleUpload = async (path: string, kind: 'agent' | 'group') => {
    if (actionNoticeTimerRef.current !== null) {
      window.clearTimeout(actionNoticeTimerRef.current);
      actionNoticeTimerRef.current = null;
    }
    setActionError(null);
    setActionNotice(null);
    setUploadError(null);
    setGroupUploadError(null);
    try {
      const result = kind === 'group'
        ? await groupClient.importGroup(path)
        : await client.importAgentTemplate(path);
      if (kind === 'group') {
        await loadGroups('mine');
        setMineKind('group');
        setGroupMineQuery('');
        setGroupMinePage(1);
      } else {
        await loadCatalog();
        setMineKind('agent');
        setMineQuery('');
      }
      setUploadDialogOpen(false);
      setUploadError(null);
      setGroupUploadDialogOpen(false);
      setView('mine');
      showActionNotice(t(kind === 'group' ? 'agentManagement.group.states.uploadSuccess' : 'agentManagement.states.uploadSuccess', { id: result.id }));
    } catch (error) {
      const message = formatActionError(error, t(kind === 'group' ? 'agentManagement.group.states.uploadError' : 'agentManagement.states.uploadError'));
      if (kind === 'group') setGroupUploadError(message);
      else setUploadError(message);
    }
  };

  const goBackToCatalog = () => {
    setActionError(null);
    setActionNotice(null);
    setView(detailOrigin);
  };

  const goBackToGroupCatalog = () => {
    setActionError(null);
    setActionNotice(null);
    setView(groupDetailOrigin);
  };

  const pendingConnectorModals = (
    <>
      <PendingConnectorModals flow={installFlow} />
      <PendingConnectorModals flow={reconnectFlow} />
    </>
  );

  const uploadDialog = uploadDialogOpen ? (
    <DefinitionUploadDialog
      initialKind="agent"
      error={uploadError || groupUploadError}
      onCancel={() => {
        setUploadDialogOpen(false);
        setUploadError(null);
        setGroupUploadError(null);
      }}
      onConfirm={handleUpload}
    />
  ) : null;

  const groupUploadDialog = groupUploadDialogOpen ? (
    <DefinitionUploadDialog
      initialKind="group"
      error={groupUploadError || uploadError}
      onCancel={() => {
        setGroupUploadDialogOpen(false);
        setGroupUploadError(null);
        setUploadError(null);
      }}
      onConfirm={handleUpload}
    />
  ) : null;

  const isMine = view === 'mine';
  const isGroupView = view === 'teams' || (isMine && mineKind === 'group');
  const panelViewClass = view === 'detail' || view === 'group-detail'
    ? 'detail'
    : view === 'create' || view === 'group-create'
      ? 'create'
      : isMine
        ? 'mine'
        : view === 'teams'
          ? 'teams'
          : 'catalog';
  return (
    <div className="app-page-body">
      <main
        className={`page-content agent-management-panel agent-management-panel--${panelViewClass}`}
        data-source={client.source}
        data-testid="agent-management-panel"
        data-variant={panelViewClass}
        data-definition-kind={isGroupView || view === 'group-detail' || view === 'group-create' ? 'group' : 'agent'}
      >
      {(view === 'mine' || view === 'catalog' || view === 'teams') && (
        <>
        <div className="page-shell flex-none">
          <PageHeader title={t('agentManagement.title')} subtitle={t('agentManagement.subtitle')} />
          <div className="page-toolbar" data-testid="page-toolbar">
            <Tabs
              role="tablist"
              ariaLabel={t('agentManagement.tabsLabel')}
              wrapperTestId="agent-management-primary-tabs"
              itemTestId="agent-management-primary-tab"
              className="h-[34px] text-base"
              value={isMine ? 'mine' : view}
              onChange={(nextView) => {
                setCreateMenuOpen(false);
                setActionError(null);
                setActionNotice(null);
                if (nextView === 'teams') setGroupCategory('');
                setView(nextView as 'catalog' | 'teams' | 'mine');
              }}
              items={[
                { value: 'catalog', label: t('agentManagement.tabs.catalog') },
                { value: 'teams', label: t('agentManagement.tabs.teams') },
                { value: 'mine', label: t('agentManagement.tabs.mine') },
              ]}
            />
            {isMine ? (
              <Tabs
                role="tablist"
                ariaLabel={t('agentManagement.tabs.mineLabel')}
                wrapperTestId="agent-management-secondary-tabs"
                itemTestId="agent-management-secondary-tab"
                className="agent-management-secondary-tabs text-base"
                value={mineKind}
                onChange={(nextKind) => setMineKind(nextKind as 'agent' | 'group')}
                items={[
                  { value: 'agent', label: t('agentManagement.tabs.mineAgent') },
                  { value: 'group', label: t('agentManagement.tabs.mineGroup') },
                ]}
              />
            ) : null}
            <div className="agent-management-primary-actions" data-testid="agent-management-primary-actions">
              <PageToolbarSearch
                wrapperTestId="agent-management-search"
                inputTestId="agent-management-search-input"
                name="agent-management-search"
                aria-label={t(view === 'teams' ? 'agentManagement.searchTeams' : isGroupView ? 'agentManagement.searchMineGroup' : isMine ? 'agentManagement.searchMine' : 'agentManagement.searchCatalog')}
                autoComplete="off"
                disabled={connectorFlowId !== null}
                value={view === 'teams' ? groupCatalogQuery : isGroupView ? groupMineQuery : isMine ? mineQuery : query}
                onChange={(e) => {
                  const nextValue = e.target.value;
                  if (view === 'teams') {
                    setGroupCatalogQuery(nextValue);
                    setGroupCatalogPage(1);
                  } else if (isGroupView) {
                    setGroupMineQuery(nextValue);
                    setGroupMinePage(1);
                  } else if (isMine) {
                    setMineQuery(nextValue);
                  } else {
                    setQuery(nextValue);
                  }
                }}
                onClear={() => {
                  if (view === 'teams') {
                    setGroupCatalogQuery('');
                    setGroupCatalogPage(1);
                  } else if (isGroupView) {
                    setGroupMineQuery('');
                    setGroupMinePage(1);
                  } else if (isMine) {
                    setMineQuery('');
                  } else {
                    setQuery('');
                  }
                }}
                placeholder={t(view === 'teams' ? 'agentManagement.searchTeams' : isGroupView ? 'agentManagement.searchMineGroup' : isMine ? 'agentManagement.searchMine' : 'agentManagement.searchCatalog')}
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
                    <div className="dropdown-menu" role="menu" data-testid="agent-management-create-menu-popover">
                      <button
                        type="button"
                        role="menuitem"
                        className="dropdown-menu-item"
                        data-testid="agent-management-create-menu-item"
                        data-variant="create-first"
                        onClick={isGroupView ? openGroupCreate : openCreate}
                      >
                        {t(isGroupView ? 'agentManagement.group.actions.createFirst' : 'agentManagement.actions.createFirst')}
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        className="dropdown-menu-item"
                        data-testid="agent-management-create-menu-item"
                        data-variant="create-by-chat"
                        onClick={() => {
                          setCreateMenuOpen(false);
                          (isGroupView ? onCreateGroupViaChat : onCreateViaChat)?.();
                        }}
                      >
                        {t(isGroupView ? 'agentManagement.group.actions.createByChat' : 'agentManagement.actions.createByChat')}
                      </button>
                      <button type="button" role="menuitem" className="dropdown-menu-item" data-testid="agent-management-create-menu-item" data-variant="create-by-upload" onClick={isGroupView ? openGroupUpload : openUpload}>
                        {t(isGroupView ? 'agentManagement.group.actions.createByUpload' : 'agentManagement.actions.createByUpload')}
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
          {isGroupView ? (
            <GroupCatalogPage
              scope={view === 'teams' ? 'catalog' : 'mine'}
              items={view === 'teams' ? groupCatalogView.items : groupMineView.items}
              totalItems={view === 'teams' ? groupCatalogView.totalItems : groupMineView.totalItems}
              page={view === 'teams' ? groupCatalogView.page : groupMineView.page}
              totalPages={view === 'teams' ? groupCatalogView.totalPages : groupMineView.totalPages}
              query={view === 'teams' ? groupCatalogQuery : groupMineQuery}
              category={view === 'teams' ? groupCategory : ''}
              status={view === 'teams' ? groupCatalogStatus : groupMineStatus}
              error={view === 'teams' ? groupCatalogError : groupMineError}
              busyId={busyId}
              onCategoryChange={value => { setGroupCategory(value); setGroupCatalogPage(1); }}
              onPageChange={value => view === 'teams' ? setGroupCatalogPage(value) : setGroupMinePage(value)}
              onRetry={() => void loadGroups(view === 'teams' ? 'catalog' : 'mine')}
              onOpen={openGroupDetail}
              onUse={handleUseGroup}
              onInstall={handleInstallGroup}
              onUninstall={handleUninstallGroup}
              onCreate={openGroupCreate}
            />
          ) : (
            <CatalogPage
              scope={isMine ? 'mine' : 'catalog'}
              items={isMine ? mineView.items : catalogView.items}
              totalItems={isMine ? mineView.totalItems : catalogView.totalItems}
              query={isMine ? mineQuery : query}
              category={category}
              status={state.catalogStatus}
              error={state.catalogError}
              busyId={busyId}
              onCategoryChange={setCategory}
              onRetry={loadCatalog}
              onOpen={openDetail}
              onUse={handleUse}
              onReconnect={handleReconnect}
              onInstall={handleInstall}
              onUninstall={handleUninstall}
              onCreate={openCreate}
            />
          )}
        </>
      )}
      {view === 'detail' && (
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
            void loadFiles(selectedId).then(files => {
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
      )}
      {view === 'create' && (
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
          onCreateGroup={openGroupCreate}
          onCancel={() => {
            setActionError(null);
            setActionNotice(null);
            setView('mine');
          }}
          onSave={handleCreate}
        />
      )}
      {view === 'group-detail' && (
        <AgentGroupDetailPage
          detail={groupDetail}
          detailStatus={groupDetailStatus}
          detailError={groupDetailError}
          detailTab={groupDetailTab}
          files={groupFiles}
          filesStatus={groupFilesStatus}
          filesError={groupFilesError}
          selectedFilePath={groupSelectedFilePath}
          fileContent={groupFileContent}
          fileStatus={groupFileStatus}
          fileError={groupFileError}
          actionError={actionError}
          actionNotice={actionNotice}
          busy={busyId === groupSelectedId}
          onBack={goBackToGroupCatalog}
          onRetry={() => groupSelectedId && void openGroupDetail(groupSelectedId)}
          onTabChange={handleGroupTabChange}
          onRetryFiles={() => groupSelectedId && void loadGroupFiles(groupSelectedId).then(files => { const first = files ? findFirstPreviewableFile(files) : null; if (first) void handleSelectGroupFile(first); })}
          onSelectFile={handleSelectGroupFile}
          onUse={handleUseGroup}
          onUsePrompt={handleUseGroupPrompt}
          onInstall={handleInstallGroup}
          onUninstall={handleUninstallGroup}
        />
      )}
      {view === 'group-create' && (
        <AgentGroupEditor
          draft={groupDraft}
          agentOptions={catalogRef.current}
          agentsStatus={state.catalogStatus}
          skillOptions={state.skillOptions}
          skillsStatus={state.skillsStatus}
          saving={groupSaving}
          error={groupCreateError}
          onChange={setGroupDraft}
          onReloadAgents={() => { void loadCatalog({ includeTeamCompatibility: true }); }}
          onReloadSkills={loadSkills}
          onCreateAgent={openCreate}
          onCancel={() => { setActionError(null); setActionNotice(null); setView('mine'); setMineKind('group'); }}
          onSave={handleGroupCreate}
        />
      )}
      {pendingConnectorModals}
      {uploadDialog}
      {groupUploadDialog}
    </main>
    </div>
  );
}
