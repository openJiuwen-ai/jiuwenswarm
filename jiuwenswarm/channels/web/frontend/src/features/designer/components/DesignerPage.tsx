import { LayoutPanelTop, Loader2, MapPinned, PanelLeft, Route, RouteOff } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import chatOrbIcon from '../../../assets/designer/xiaoyi-app-icon.png';
import {
  collectDesignerMaterials,
  collectPendingRevisions,
} from '../designerMaterials';
import { useDesignerChatStore } from '../designerChatStore';
import { seedDesignerChatHistory } from '../designerEntry';
import { bindDesignerRuntime, useDesignerRunStore } from '../designerRunStore';
import { isDesignerModerationError } from '../designerRunView';
import { isDesignerPreviewGraph } from '../designerBootstrapGraph';
import { DESIGNER_UPLOAD_TOO_LARGE } from '../designerNodeUpload';
import { bindDesignerExclusivePlayback } from '../designerMediaPlayback';
import { useDesignerGraphEditingLocked } from '../useDesignerGraphEditingLocked';
import { useDesignerStore } from '../designerStore';
import { useDesignerUiStore } from '../designerUiStore';
import { DesignerMaterialViewer } from '../DesignerMaterialViewer';
import { DesignerRevisionChooser } from '../DesignerRevisionChooser';
import { DesignerDeleteNodeDialog } from './controls/DesignerDeleteNodeDialog';
import { DesignerZoomMenu } from './DesignerZoomMenu';
import { DesignerAssetsSidebar } from './DesignerAssetsSidebar';
import { DesignerCanvas } from './DesignerCanvas';
import { DesignerChatPanel, DesignerEmptyState } from './DesignerChatPanel';
import { designerWorkspaceClient } from '../designerGraphClient';
import { useWorkspaceStore } from '../../../stores/workspaceStore';
import './DesignerErrorToast.css';
import './DesignerPage.css';

type DesignerPageProps = {
  projectId?: string;
  sessionId?: string;
  onOpenSession?: (projectId: string, sessionId: string, options?: { replace?: boolean }) => void;
  sidebarCollapsed?: boolean;
  onToggleSidebarCollapse?: () => void;
};

function startOfLocalDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function formatModifiedAt(
  timestamp: number | undefined,
  locale: string,
  yesterdayLabel: string,
): string {
  if (!timestamp || !Number.isFinite(timestamp)) return '';
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return '';

  const time = new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
  const now = new Date();
  const dayDiff = Math.round(
    (startOfLocalDay(now).getTime() - startOfLocalDay(date).getTime()) / 86_400_000,
  );
  if (dayDiff === 0) return time;
  if (dayDiff === 1) return `${yesterdayLabel} ${time}`;

  const isZh = locale.toLowerCase().startsWith('zh');
  if (date.getFullYear() === now.getFullYear()) {
    if (isZh) return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
    return `${new Intl.DateTimeFormat(locale, { month: 'short', day: 'numeric' }).format(date)} ${time}`;
  }
  if (isZh) {
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 ${time}`;
  }
  return `${new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  }).format(date)} ${time}`;
}

export function DesignerPage({
  projectId,
  sessionId,
  onOpenSession,
  sidebarCollapsed = false,
  onToggleSidebarCollapse,
}: DesignerPageProps) {
  const { t, i18n } = useTranslation();
  const effectiveProjectId = projectId;

  const loadStatus = useDesignerStore((state) => state.loadStatus);
  const loadError = useDesignerStore((state) => state.loadError);
  const domainGraph = useDesignerStore((state) => state.domainGraph);
  const graphId = useDesignerStore((state) => state.graphId);
  const bootstrapInProgress = useDesignerStore((state) => state.bootstrapInProgress);
  const resetForGraph = useDesignerRunStore((state) => state.resetForGraph);
  const boundGraphId = useDesignerRunStore((state) => state.boundGraphId);
  const run = useDesignerRunStore((state) => state.run);
  const runError = useDesignerRunStore((state) => state.runError);
  const runWarning = useDesignerRunStore((state) => state.runWarning);
  const chooseOutput = useDesignerRunStore((state) => state.chooseOutput);
  const selectedMaterialId = useDesignerUiStore((state) => state.selectedMaterialId);
  const viewerOpen = useDesignerUiStore((state) => state.viewerOpen);
  const chooserNodeId = useDesignerUiStore((state) => state.chooserNodeId);
  const editRequestKey = useDesignerUiStore((state) => state.editRequestKey);
  const setSelectedMaterialId = useDesignerUiStore((state) => state.setSelectedMaterialId);
  const closeViewer = useDesignerUiStore((state) => state.closeViewer);
  const closeRevision = useDesignerUiStore((state) => state.closeRevision);
  const resetUi = useDesignerUiStore((state) => state.reset);
  const chatSidebarOpen = useDesignerUiStore((state) => state.chatSidebarOpen);
  const assetsSidebarOpen = useDesignerUiStore((state) => state.assetsSidebarOpen);
  const minimapVisible = useDesignerUiStore((state) => state.minimapVisible);
  const edgesVisible = useDesignerUiStore((state) => state.edgesVisible);
  const toggleChatSidebar = useDesignerUiStore((state) => state.toggleChatSidebar);
  const toggleMinimap = useDesignerUiStore((state) => state.toggleMinimap);
  const toggleEdges = useDesignerUiStore((state) => state.toggleEdges);
  const requestAutoLayout = useDesignerUiStore((state) => state.requestAutoLayout);
  const pendingDeleteNodeIds = useDesignerUiStore((state) => state.pendingDeleteNodeIds);
  const dismissDeleteNodes = useDesignerUiStore((state) => state.dismissDeleteNodes);
  const closeDock = useDesignerUiStore((state) => state.closeDock);
  const removeNodes = useDesignerStore((state) => state.removeNodes);
  const confirmDeleteNodes = useCallback(() => {
    const ids = useDesignerUiStore.getState().pendingDeleteNodeIds;
    dismissDeleteNodes();
    if (ids.length === 0) return;
    removeNodes(ids);
    closeDock();
  }, [closeDock, dismissDeleteNodes, removeNodes]);
  const workspaceLoadSeqRef = useRef(0);
  const loadedKeyRef = useRef('');
  const onOpenSessionRef = useRef(onOpenSession);
  onOpenSessionRef.current = onOpenSession;
  useEffect(() => bindDesignerRuntime(), []);

  useEffect(() => {
    if (!effectiveProjectId || bootstrapInProgress) return;
    const requestedKey = `${effectiveProjectId}:${sessionId || ''}`;
    if (sessionId && loadedKeyRef.current === requestedKey) return;
    const loadSeq = ++workspaceLoadSeqRef.current;
    // The composer hands over this session's graph before navigating while
    // the first leader round is already pushing designer.graph.updated;
    // resetting would drop those pushes until workspace.get returns.
    const held = useDesignerStore.getState().domainGraph;
    if (!sessionId || held?.metadata?.session_id !== sessionId) {
      useDesignerStore.getState().reset();
    }
    void designerWorkspaceClient.get(effectiveProjectId, sessionId)
      .then((workspace) => {
        if (workspaceLoadSeqRef.current !== loadSeq) return;
        const opened = String(workspace.session?.session_id || '');
        loadedKeyRef.current = `${effectiveProjectId}:${opened}`;
        const current = useDesignerStore.getState().domainGraph;
        const newer = current?.graph_id === workspace.graph.graph_id
          && (current.updated_at ?? 0) > (workspace.graph.updated_at ?? 0);
        if (!newer) useDesignerStore.getState().applyGraph(workspace.graph);
        useDesignerChatStore.getState().replaceMessages(
          workspace.graph.graph_id,
          workspace.messages,
        );
        if (opened) seedDesignerChatHistory(opened, workspace.messages);
        if (opened && opened !== sessionId) {
          onOpenSessionRef.current?.(effectiveProjectId, opened, { replace: true });
        }
      })
      .catch((reason) => {
        if (workspaceLoadSeqRef.current !== loadSeq) return;
        const message = reason instanceof Error ? reason.message : String(reason);
        useDesignerStore.getState().failBootstrapEntry(message);
      });
    return () => {
      if (workspaceLoadSeqRef.current === loadSeq) {
        workspaceLoadSeqRef.current += 1;
      }
    };
  }, [
    bootstrapInProgress,
    effectiveProjectId,
    sessionId,
  ]);

  useEffect(() => {
    const nextId = domainGraph?.graph_id ?? null;
    if (nextId !== boundGraphId) {
      resetUi();
      resetForGraph(domainGraph);
      useDesignerChatStore.getState().bindGraph(nextId);
    }
    if (domainGraph && !isDesignerPreviewGraph(domainGraph)) {
      useDesignerChatStore.getState().ensureGraphPrompt(domainGraph, {
        doneText: t('designer.chat.bootstrapDone'),
      });
    }
  }, [boundGraphId, domainGraph, resetForGraph, resetUi, t]);

  useEffect(() => {
    if (!effectiveProjectId) return;
    const workspace = useWorkspaceStore.getState();
    void workspace.loadProjectSessions(effectiveProjectId);
    void workspace.loadProjects();
  }, [domainGraph?.graph_id, domainGraph?.title, effectiveProjectId]);

  const materials = useMemo(
    () => collectDesignerMaterials(domainGraph, run),
    [domainGraph, run],
  );
  const pendingRevisions = useMemo(
    () => collectPendingRevisions(domainGraph, run),
    [domainGraph, run],
  );
  const autoPromotedRef = useRef(new Set<string>());
  useEffect(() => {
    const runId = run?.run_id;
    if (!runId || run?.status === 'running') return;
    for (const item of pendingRevisions) {
      const key = `${runId}:${item.nodeId}`;
      if (item.requiresSelection || autoPromotedRef.current.has(key)) continue;
      // Brief/storyboard versions remain pending until the user chooses one.
      autoPromotedRef.current.add(key);
      void chooseOutput(item.nodeId, 'new').then(() => {
        if (chooserNodeId === item.nodeId) closeRevision();
      });
    }
  }, [chooseOutput, chooserNodeId, closeRevision, pendingRevisions, run?.run_id, run?.status]);
  const activeRevision =
    pendingRevisions.find((item) => item.nodeId === chooserNodeId) ?? pendingRevisions[0];

  const graphReady = Boolean(domainGraph) && (!graphId || domainGraph?.graph_id === graphId);
  const showCanvas = graphReady;
  const showEmpty = !showCanvas && loadStatus === 'empty';
  const showError = !showCanvas && loadStatus === 'error';
  const showLoading =
    !showCanvas &&
    (loadStatus === 'loading' || loadStatus === 'idle' || loadStatus === 'bootstrapping');

  const projectTitle =
    domainGraph?.title?.trim() ||
    (showLoading || showEmpty || showError ? '' : t('designer.subtitle'));
  const modifiedLabel = formatModifiedAt(
    domainGraph?.updated_at,
    i18n.language,
    t('designer.toolbar.yesterday'),
  );
  const canvasInteractive =
    showCanvas && !bootstrapInProgress && !isDesignerPreviewGraph(domainGraph);
  const editingLocked = useDesignerGraphEditingLocked();

  // The canvas has three independent players (node cards, the material viewer
  // which autoplays, the revision chooser). Keep exactly one of them audible so
  // opening the viewer never overlaps a running node preview (#5411).
  const pageRef = useRef<HTMLDivElement>(null);
  useEffect(() => bindDesignerExclusivePlayback(pageRef.current), []);

  return (
    <div className="designer-page app-section" data-testid="designer-page" ref={pageRef}>
      <div className="designer-page__main">
        <header className="designer-page__toolbar" data-testid="designer-page-toolbar">
          {onToggleSidebarCollapse ? (
            <button
              type="button"
              className="designer-page__sidebar-collapse"
              onClick={onToggleSidebarCollapse}
              aria-label={sidebarCollapsed ? t('common.expand') : t('common.collapse')}
              aria-pressed={!sidebarCollapsed}
              title={sidebarCollapsed ? t('common.expand') : t('common.collapse')}
              data-testid="designer-page-sidebar-collapse"
              data-variant={sidebarCollapsed ? 'expand' : 'collapse'}
            >
              <PanelLeft size={18} aria-hidden />
            </button>
          ) : null}
          <div className="designer-page__heading">
            <h1 className="designer-page__title" data-testid="designer-page-title">
              {projectTitle || t('designer.subtitle')}
            </h1>
            {modifiedLabel ? (
              <span className="designer-page__modified" data-testid="designer-page-modified">
                {modifiedLabel}
              </span>
            ) : null}
          </div>
          <div className="designer-page__toolbar-actions">
            <DesignerZoomMenu />
            <span className="designer-page__toolbar-split" aria-hidden />
            <button
              type="button"
              className="designer-page__toolbar-icon-btn"
              aria-label={edgesVisible ? t('designer.toolbar.hideEdges') : t('designer.toolbar.showEdges')}
              aria-pressed={edgesVisible}
              title={edgesVisible ? t('designer.toolbar.hideEdges') : t('designer.toolbar.showEdges')}
              data-testid="designer-page-edges-toggle"
              disabled={!canvasInteractive}
              onClick={toggleEdges}
            >
              {edgesVisible ? <Route size={18} aria-hidden /> : <RouteOff size={18} aria-hidden />}
            </button>
            <button
              type="button"
              className={`designer-page__toolbar-icon-btn${editingLocked ? ' designer-edit-control' : ''}`}
              aria-label={t('designer.toolbar.layoutNodes')}
              title={t('designer.toolbar.layoutNodes')}
              data-testid="designer-page-layout"
              disabled={!canvasInteractive || editingLocked || !domainGraph?.nodes.length}
              onClick={requestAutoLayout}
            >
              <LayoutPanelTop size={18} aria-hidden />
            </button>
            <button
              type="button"
              className={`designer-page__toolbar-icon-btn${minimapVisible ? ' is-active' : ''}`}
              aria-label={t('designer.toolbar.minimap')}
              aria-pressed={minimapVisible}
              title={t('designer.toolbar.minimap')}
              data-testid="designer-page-minimap-toggle"
              disabled={!canvasInteractive}
              onClick={toggleMinimap}
            >
              <MapPinned size={18} aria-hidden />
            </button>
            <button
              type="button"
              className="designer-page__chat-btn"
              aria-label={t('designer.toolbar.openChat')}
              aria-pressed={chatSidebarOpen}
              title={t('designer.toolbar.openChat')}
              data-testid="designer-page-chat-toggle"
              onClick={toggleChatSidebar}
            >
              <img src={chatOrbIcon} alt="" width={20} height={20} />
              <span>{t('designer.toolbar.openChat')}</span>
            </button>
          </div>
        </header>
        {runError ? (
          <div
            className="app-toast-wrapper app-toast-wrapper--top-center"
            data-testid="designer-run-error-toast"
          >
            <div className="designer-error-toast" role="alert" data-testid="designer-error" title={runError}>
              {runError === DESIGNER_UPLOAD_TOO_LARGE
                ? t('designer.dock.uploadTooLarge')
                : isDesignerModerationError(runError)
                  ? t('designer.run.moderationBlocked')
                  : runError}
            </div>
          </div>
        ) : null}
        {runWarning ? (
          <p className="designer-page__warning" data-testid="designer-warning">
            {runWarning}
          </p>
        ) : null}

        <div className="designer-page__canvas-area">
          {showCanvas && domainGraph ? <DesignerCanvas graph={domainGraph} /> : null}

          {showLoading ? (
            <div className="designer-page__state" data-testid="designer-loading-state">
              <div className="designer-page__state-card">
                <Loader2 className="mx-auto mb-3 animate-spin" size={24} aria-hidden />
                <p className="designer-page__state-desc">
                  {loadStatus === 'bootstrapping' ? t('designer.chat.thinking') : t('designer.loading')}
                </p>
              </div>
            </div>
          ) : null}

          {showEmpty ? <DesignerEmptyState variant="empty" /> : null}
          {showError ? <DesignerEmptyState variant="error" errorMessage={loadError} /> : null}
        </div>
      </div>

      {assetsSidebarOpen ? <DesignerAssetsSidebar /> : null}
      {chatSidebarOpen ? <DesignerChatPanel /> : null}

      {viewerOpen ? (
        <DesignerMaterialViewer
          materials={materials}
          selectedId={selectedMaterialId}
          startEditKey={editRequestKey}
          onSelect={setSelectedMaterialId}
          onClose={closeViewer}
        />
      ) : null}
      {activeRevision && chooserNodeId ? (
        <DesignerRevisionChooser
          revision={activeRevision}
          busy={Boolean(run?.status === 'running')}
          onChoose={(choice) => {
            void chooseOutput(activeRevision.nodeId, choice).then(() => closeRevision());
          }}
          onClose={closeRevision}
        />
      ) : null}
      {pendingDeleteNodeIds.length > 0 ? (
        <DesignerDeleteNodeDialog onCancel={dismissDeleteNodes} onConfirm={confirmDeleteNodes} />
      ) : null}
    </div>
  );
}
