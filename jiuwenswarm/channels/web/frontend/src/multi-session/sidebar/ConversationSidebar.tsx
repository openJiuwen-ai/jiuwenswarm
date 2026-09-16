import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal, flushSync } from 'react-dom';
import { Archive, Check, ChevronDown, CircleAlert, Code2, LoaderCircle, Workflow } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { useChatStore, type ChatRuntime } from '../../stores/chatStore';
import { webClient } from '../../services/webClient';
import { getArchiveErrorCode, archivedTaskClient, findBatchSessionResult, parseProjectOperationFailure } from '../../features/workspace/archivedTaskClient';
import { requestSettingsModule } from '../../features/settings/settingsNavigation';
import { DeleteDialog } from '../dialogs/Dialogs';
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger, toast } from '../../components/ui';
import {
  PROJECT_SESSION_PAGE_SIZE,
  useWorkspaceStore,
  useCronStore,
  filterJobsForProject,
  type SidebarCronJob,
} from '../../stores';
import type { AgentMode, ProjectInfo, Session } from '../../types';
import {
  getConversationMenuItems,
  getProjectNewLabel,
  getProjectMenuItems,
  getProjectSessionMenuItems,
  getSessionActivityAt,
  getSessionIndicator,
  getTaskStatusLabel,
  sortSessionsForSidebar,
  type SidebarMenuAction,
  type SidebarMenuItem,
} from './sidebarModel';
import { ProjectCreateMenu } from './ProjectCreateMenu';
import { projectCreateErrorKey } from './projectCreateErrors';
import { projectRegistryClient } from '../../features/workspace/projectRegistryClient';
import {
  isLikelyAbsolutePath,
  isProjectDirectoryPickerSupported,
  selectProjectDirectory,
} from '../../features/workspace/projectDirectoryPicker';
import { toDisplaySessionTitle } from '../../utils/documentMessage';
import './ConversationSidebar.css';
import '../dialogs/dialogs.css';
import AddProjectIcon from '../../assets/work-mode/add-project.svg?react';
import ArrowRightIcon from '../../assets/work-mode/arrow-right.svg?react';
import CollapseIcon from '../../assets/work-mode/collapse.svg?react';
import CloseIcon from '../../assets/work-mode/close.svg?react';
import CronIcon from '../../assets/定时任务.svg?react';
import DeleteIcon from '../../assets/work-mode/delete.svg?react';
import EditIcon from '../../assets/work-mode/edit.svg?react';
import FolderFoldIcon from '../../assets/work-mode/folder-fold.svg?react';
import FolderIcon from '../../assets/work-mode/folder.svg?react';
import MoreIcon from '../../assets/work-mode/more-rimless.svg?react';
import NewTaskIcon from '../../assets/work-mode/new-task.svg?react';
import PinIcon from '../../assets/work-mode/pin.svg?react';
import PlusIcon from '../../assets/work-mode/plus.svg?react';
import UnpinIcon from '../../assets/work-mode/unpin.svg?react';
import PanelCollapseIcon from '../../assets/panel-collapse.svg?react';

const UNREAD_KEY = 'jiuwenswarm_session_unread';
const RELATIVE_TIME_REFRESH_MS = 60_000;

export type NewConversationOptions = {
  preserveProject?: boolean;
  project?: Pick<ProjectInfo, 'project_id' | 'project_dir'>;
  /** 进入新对话时预填到输入框的文本（例如"通过聊天创建定时任务"引导语），见 App.tsx enterNewConversation */
  initialInputValue?: string;
  /** 进入新对话时一次性预选的技能，随首条消息迁移到真实会话。 */
  initialSelectedSkills?: string[];
  /** 扩展详情页"使用"按钮跳转——进入新对话时顺带打开这些插件/MCP 的会话内启用开关，
   * 见 App.tsx enterNewConversation。 */
  initialEnabledPlugins?: string[];
  initialEnabledMcps?: string[];
  /** 专家团「通过聊天创建」入口使用 4.9 高保真欢迎态。 */
  welcomeVariant?: 'group-create';
  /**
   * 强制新会话进入指定模式，覆盖"继承当前活动会话模式"的默认行为，也覆盖未发送的临时新会话
   * 草稿里残留的模式。用于扩展页"使用插件/使用 MCP/试试这样用"这类入口——插件/MCP 不支持
   * 集群模式，跳转会话时必须回到单 agent 模式（bug003）。见 App.tsx enterNewConversation。
   */
  forceMode?: AgentMode;
  /**
   * 进入新对话时的一次性会话 metadata，随首条消息经 chat.send 发送后清除。MCP 推荐问题
   * 等场景用「prefer_mcp」把「优先使用哪个 MCP」这类后台意图透传给后端，见 App.tsx
   * enterNewConversation / onUseExample。
   */
  metadata?: Record<string, unknown>;
};

function isDefaultProject(project: ProjectInfo): boolean {
  return project.is_default || project.project_id === 'default' || project.project_id === 'default_code';
}

interface ConversationSidebarProps {
  activeSessionId: string | null;
  onNew: (options?: NewConversationOptions) => void;
  onSelect: (session: Session) => void;
  /** 跳转到"定时任务"主面板；该入口原来在最左侧图标栏，现移到工作小窗口的"新建任务"下方 */
  onOpenCron: () => void;
  /** 当前是否正停留在定时任务面板，用于给下面这个入口按钮加选中态 */
  isCronActive: boolean;
  /** 侧边栏是否收起 */
  collapsed?: boolean;
  /** 小屏下侧边栏脱离文档流浮动 */
  floating?: boolean;
  /** 切换侧边栏收起/展开 */
  onToggleCollapse?: () => void;
}

interface ConversationListItemProps {
  session: Session;
  runtime?: ChatRuntime;
  active: boolean;
  nested: boolean;
  unread: boolean;
  now: number;
  onSelect: () => void;
  onPin: () => void;
  onRename: () => void;
  onArchive: () => void;
  menuItems: SidebarMenuItem[];
}

export function getProcessingTransitions(
  previous: Record<string, boolean>,
  sessions: Session[],
  runtimes: Record<string, ChatRuntime>,
  activeSessionId: string | null,
): { snapshot: Record<string, boolean>; completedInBackground: string[] } {
  const snapshot: Record<string, boolean> = {};
  const completedInBackground: string[] = [];
  for (const session of sessions) {
    const sessionId = session.session_id;
    const processing = runtimes[sessionId]?.isProcessing ?? session.is_processing === true;
    snapshot[sessionId] = processing;
    if (previous[sessionId] && !processing && sessionId !== activeSessionId) {
      completedInBackground.push(sessionId);
    }
  }
  return { snapshot, completedInBackground };
}

export function formatRelativeTime(
  activityAt: number,
  now: number,
  language: string,
  translate: (key: string, options?: Record<string, unknown>) => string,
): string {
  const elapsed = Math.max(0, now - activityAt);
  if (elapsed < 60_000) return translate('time.justNow');
  if (elapsed < 3_600_000) return translate('time.minutesAgo', { count: Math.floor(elapsed / 60_000) });
  if (elapsed < 86_400_000) return translate('time.hoursAgo', { count: Math.floor(elapsed / 3_600_000) });
  if (elapsed < 604_800_000) return translate('time.daysAgo', { count: Math.floor(elapsed / 86_400_000) });
  return new Date(activityAt).toLocaleDateString(language, { month: 'short', day: 'numeric' });
}

function getSessionTitle(session: Session, fallback: string): string {
  const raw = session.display_title?.trim() || session.title?.trim() || fallback;
  return toDisplaySessionTitle(raw) || fallback;
}

const menuIconByAction: Record<SidebarMenuAction, React.ComponentType<React.SVGProps<SVGSVGElement>>> = {
  pin: PinIcon,
  rename: EditIcon,
  archive: Archive,
  delete: DeleteIcon,
  'archive-sessions': FolderIcon,
};

function getMenuIcon(item: SidebarMenuItem): React.ComponentType<React.SVGProps<SVGSVGElement>> {
  if (item.action === 'pin' && item.pinned) return UnpinIcon;
  return menuIconByAction[item.action];
}

/** 领域模型 SidebarMenuItem 到通用 DropdownMenuItem 的映射；会话行与项目行两处菜单共用 */
function SidebarDropdownItems({
  items,
  onAction,
}: {
  items: SidebarMenuItem[];
  onAction: (action: SidebarMenuAction) => void;
}) {
  return (
    <>
      {items.map((item) => {
        const MenuIcon = getMenuIcon(item);
        return (
          <DropdownMenuItem
            key={item.action}
            icon={<MenuIcon aria-hidden />}
            danger={item.danger}
            onSelect={() => onAction(item.action)}
            data-testid="multi-session-conversation-menu-item"
            data-variant={item.action}
          >
            {item.label}
          </DropdownMenuItem>
        );
      })}
    </>
  );
}

function loadUnreadSessions(): Set<string> {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(UNREAD_KEY) || '[]');
    if (!Array.isArray(value)) return new Set();
    return new Set(value.filter((id): id is string => typeof id === 'string'));
  } catch {
    return new Set();
  }
}

function ConversationListItem({
  session,
  runtime,
  active,
  nested,
  unread,
  now,
  onSelect,
  onPin,
  onRename,
  onArchive,
  menuItems,
}: ConversationListItemProps) {
  const { t, i18n } = useTranslation();
  const itemRef = useRef<HTMLDivElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const { tooltip: itemTooltip, handlers: itemTooltipHandlers } = useAdaptiveTooltip();
  const { tooltip: truncationTooltip, handlers: truncationTooltipHandlers } = useAdaptiveTooltip({
    anchorRef: itemRef,
    offsetY: 2,
    align: 'left',
  });
  const title = getSessionTitle(session, t('multiSession.untitled'));
  const titleTooltipHandlers = {
    onMouseEnter: (event: React.MouseEvent<HTMLSpanElement>) => {
      const el = event.currentTarget;
      el.setAttribute('data-tooltip', el.scrollWidth > el.clientWidth ? title : '');
      truncationTooltipHandlers.onMouseEnter(event);
    },
    onMouseLeave: (event: React.MouseEvent<HTMLSpanElement>) => {
      event.currentTarget.setAttribute('data-tooltip', '');
      truncationTooltipHandlers.onMouseLeave();
    },
    onFocus: (event: React.FocusEvent<HTMLSpanElement>) => {
      const el = event.currentTarget;
      el.setAttribute('data-tooltip', el.scrollWidth > el.clientWidth ? title : '');
      truncationTooltipHandlers.onFocus(event);
    },
    onBlur: (event: React.FocusEvent<HTMLSpanElement>) => {
      event.currentTarget.setAttribute('data-tooltip', '');
      truncationTooltipHandlers.onBlur();
    },
  };
  const errorMessage = runtime?.error || runtime?.executionError || null;
  const indicator = getSessionIndicator(runtime, unread, session.is_processing === true, Boolean(errorMessage));

  let status: React.ReactNode;
  if (indicator === 'waiting') {
    status = (
      <span className="conversation-list-item__status-waiting" title={getTaskStatusLabel(indicator, t)} data-testid="multi-session-conversation-list-item-status-waiting">
        <span>{t('multiSession.status.waiting')}</span>
      </span>
    );
  } else if (indicator === 'processing') {
    status = (
      <span title={getTaskStatusLabel(indicator, t)} data-testid="multi-session-conversation-list-item-status-processing">
        <LoaderCircle className="conversation-list-item__loader" aria-hidden="true" />
      </span>
    );
  } else if (indicator === 'unread') {
    status = <span className="conversation-list-item__status-dot" title={t('multiSession.completedUnread')} aria-hidden="true" data-testid="multi-session-conversation-list-item-status-unread" />;
  } else if (indicator === 'error') {
    status = (
      <span title={errorMessage ?? getTaskStatusLabel(indicator, t)} data-testid="multi-session-conversation-list-item-status-error">
        <CircleAlert className="conversation-list-item__status-error" size={14} strokeWidth={1.8} aria-hidden="true" />
      </span>
    );
  } else {
    status = (
      <span className="conversation-list-item__status-read" title={getTaskStatusLabel(indicator, t)} data-testid="multi-session-conversation-list-item-status-read">
        <span>{formatRelativeTime(getSessionActivityAt(session), now, i18n.language, t)}</span>
      </span>
    );
  }

  return (
    <div
      ref={itemRef}
      className={`conversation-list-item conversation-sidebar__row-el${active ? ' is-active' : ''}${menuOpen ? ' is-menu-open' : ''}${nested ? ' conversation-list-item--nested' : ''}`}
      data-testid="multi-session-conversation-list-item"
      data-variant={session.session_id}
    >
      <button type="button" className="conversation-list-item__main" onClick={onSelect} data-testid="multi-session-conversation-list-item-main">
        <span className="conversation-list-item__title" data-testid="multi-session-conversation-list-item-title" data-tooltip="" {...titleTooltipHandlers}>
          {title}
        </span>
        <span className="conversation-list-item__meta" data-testid="multi-session-conversation-list-item-status" data-variant={indicator}>
          {status}
        </span>
      </button>
      <DropdownMenu
        open={menuOpen}
        onOpenChange={(open) => {
          if (open) itemTooltipHandlers.onMouseLeave();
          setMenuOpen(open);
        }}
      >
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="conversation-list-item__actions"
            onClick={(event) => event.stopPropagation()}
            aria-label={t('multiSession.moreActions')}
            data-tooltip={t('multiSession.moreActions')}
            data-testid="multi-session-conversation-list-item-more"
            {...(menuOpen ? {} : itemTooltipHandlers)}
          >
            <MoreIcon aria-hidden />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="bottom" align="end" data-testid="multi-session-conversation-menu">
          <SidebarDropdownItems
            items={menuItems}
            onAction={(action) => {
              switch (action) {
                case 'pin':
                  onPin();
                  break;
                case 'rename':
                  onRename();
                  break;
                case 'archive':
                  onArchive();
                  break;
              }
            }}
          />
        </DropdownMenuContent>
      </DropdownMenu>
      <button
        type="button"
        className="conversation-list-item__pin-action"
        onClick={(event) => {
          event.stopPropagation();
          onPin();
        }}
        aria-label={session.pinned ? t('multiSession.project.unpinConversation') : t('multiSession.project.pinConversation')}
        data-tooltip={session.pinned ? t('multiSession.project.unpinConversation') : t('multiSession.project.pinConversation')}
        data-testid="multi-session-conversation-list-item-pin"
        {...itemTooltipHandlers}
      >
        {session.pinned ? <UnpinIcon aria-hidden /> : <PinIcon aria-hidden />}
      </button>
      {itemTooltip}
      {truncationTooltip}
    </div>
  );
}

function ProjectEntityRow({
  title,
  path,
  isExpanded,
  isPinned,
  hasUnreadCronResult = false,
  defaultProject = false,
  onToggle,
  onNew,
  onPin,
  onRename,
  onRemove,
  onBatch,
  newLabel,
  projectId,
}: {
  title: string;
  path?: string;
  isExpanded: boolean;
  isPinned?: boolean;
  hasUnreadCronResult?: boolean;
  defaultProject?: boolean;
  onToggle: () => void;
  onNew: () => void;
  onPin: () => void;
  onRename: () => void;
  onRemove: () => void;
  onBatch: (action: 'archive') => void;
  newLabel?: string;
  projectId?: string;
}) {
  const { t } = useTranslation();
  const mainRef = useRef<HTMLButtonElement>(null);
  const tooltipId = useId();
  const [menuOpen, setMenuOpen] = useState(false);
  const { tooltip: rowTooltip, handlers: rowTooltipHandlers } = useAdaptiveTooltip();
  const [tooltipPos, setTooltipPos] = useState<{ left: number; top: number } | null>(null);
  // 分别跟踪 hover 与 focus 状态：任一活跃即保持 tooltip，避免 mouseleave/blur 互相误清
  const hoverRef = useRef(false);
  const focusRef = useRef(false);

  const showTooltip = (source: 'hover' | 'focus') => {
    if (source === 'hover') hoverRef.current = true;
    else focusRef.current = true;
    if (!path || !mainRef.current) return;
    const rect = mainRef.current.getBoundingClientRect();
    setTooltipPos({ left: rect.left, top: rect.bottom + 6 });
  };

  const hideTooltip = (source: 'hover' | 'focus') => {
    if (source === 'hover') hoverRef.current = false;
    else focusRef.current = false;
    if (hoverRef.current || focusRef.current) return;
    setTooltipPos(null);
  };

  return (
    <div
      className={`conversation-entity-row conversation-sidebar__row-el${menuOpen ? ' is-menu-open' : ''}`}
      data-testid="multi-session-project-row"
      data-variant={projectId}
    >
      <button
        type="button"
        ref={mainRef}
        className="conversation-entity-row__main"
        onClick={(event) => {
          onToggle();
          // 鼠标点击（detail>0）展开/收起后立即收起路径提示，避免浮层残留；键盘触发的点击保留 focus 提示
          if (event.detail > 0) {
            hoverRef.current = false;
            setTooltipPos(null);
          }
        }}
        title={path ? undefined : title}
        aria-describedby={path ? tooltipId : undefined}
        onMouseEnter={() => showTooltip('hover')}
        onMouseLeave={() => hideTooltip('hover')}
        onFocus={() => {
          // 仅键盘导航（:focus-visible）显示 focus 提示；鼠标点击也会触发 focus，
          // 若不区分会导致点击后 focusRef 残留为 true，鼠标移出时 tooltip 无法消失
          if (mainRef.current?.matches(':focus-visible')) showTooltip('focus');
        }}
        onBlur={() => hideTooltip('focus')}
        data-testid="multi-session-project-row-main"
      >
        <span className="conversation-entity-row__icon">
          {isExpanded ? <FolderFoldIcon aria-hidden /> : <FolderIcon aria-hidden />}
        </span>
        <span className="conversation-entity-row__text">
          <span className="conversation-entity-row__title" data-testid="multi-session-project-row-title">
            {title}
          </span>
        </span>
        {hasUnreadCronResult ? (
          <span className="conversation-list-item__status-dot" aria-hidden="true" data-testid="multi-session-project-row-cron-unread" />
        ) : null}
        {isExpanded ? <CollapseIcon className="conversation-entity-row__chevron" aria-hidden /> : <ArrowRightIcon className="conversation-entity-row__chevron" aria-hidden />}
        {isPinned ? <PinIcon className="conversation-entity-row__pin" aria-hidden /> : null}
      </button>
      <button
        type="button"
        className="conversation-entity-row__plus"
        onClick={(event) => {
          event.stopPropagation();
          onNew();
        }}
        aria-label={newLabel || t('multiSession.project.newConversation')}
        data-tooltip={newLabel || t('multiSession.project.newConversation')}
        data-testid="multi-session-project-row-new-conversation"
        {...rowTooltipHandlers}
      >
        <PlusIcon aria-hidden />
      </button>
      <DropdownMenu
        open={menuOpen}
        onOpenChange={(open) => {
          if (open) rowTooltipHandlers.onMouseLeave();
          setMenuOpen(open);
        }}
      >
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="conversation-list-item__actions"
            onClick={(event) => event.stopPropagation()}
            aria-label={t('multiSession.moreActions')}
            data-tooltip={t('multiSession.moreActions')}
            data-testid="multi-session-project-row-more"
            {...(menuOpen ? {} : rowTooltipHandlers)}
          >
            <MoreIcon aria-hidden />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="bottom" align="end" data-testid="multi-session-conversation-menu">
          <SidebarDropdownItems
            items={getProjectMenuItems(Boolean(isPinned), t, defaultProject)}
            onAction={(action) => {
              switch (action) {
                case 'pin':
                  onPin();
                  break;
                case 'rename':
                  onRename();
                  break;
                case 'delete':
                  onRemove();
                  break;
                case 'archive-sessions':
                  onBatch('archive');
                  break;
              }
            }}
          />
        </DropdownMenuContent>
      </DropdownMenu>
      {tooltipPos && path
        ? createPortal(
            <ProjectPathTooltip id={tooltipId} title={title} path={path} anchor={tooltipPos} mainRef={mainRef} />,
            document.body,
          )
        : null}
      {rowTooltip}
    </div>
  );
}

function ProjectPathTooltip({
  id,
  title,
  path,
  anchor,
  mainRef,
}: {
  id: string;
  title: string;
  path: string;
  anchor: { left: number; top: number };
  mainRef: React.RefObject<HTMLButtonElement | null>;
}) {
  const tipRef = useRef<HTMLDivElement>(null);
  const [placed, setPlaced] = useState<{ left: number; top: number } | null>(null);

  useLayoutEffect(() => {
    const tip = tipRef.current;
    const main = mainRef.current;
    if (!tip || !main) return;
    const reposition = () => {
      const tipRect = tip.getBoundingClientRect();
      const mainRect = main.getBoundingClientRect();
      const margin = 6;
      let top = mainRect.bottom + margin;
      // 下方空间不足则翻转到上方
      if (top + tipRect.height > window.innerHeight) {
        top = mainRect.top - tipRect.height - margin;
      }
      let left = mainRect.left;
      // 右侧溢出则向左收
      if (left + tipRect.width > window.innerWidth - margin) {
        left = Math.max(margin, window.innerWidth - tipRect.width - margin);
      }
      setPlaced({ left, top });
    };
    reposition();
    // 滚动/缩放/列表重排时按钮位置会变，需重算定位
    window.addEventListener('scroll', reposition, true);
    window.addEventListener('resize', reposition);
    return () => {
      window.removeEventListener('scroll', reposition, true);
      window.removeEventListener('resize', reposition);
    };
  }, [mainRef]);

  return (
    <div
      ref={tipRef}
      id={id}
      className="project-path-tooltip"
      role="tooltip"
      data-testid="multi-session-project-path-tooltip"
      style={{
        position: 'fixed',
        left: placed ? placed.left : anchor.left,
        top: placed ? placed.top : anchor.top,
        opacity: placed ? 1 : 0,
      }}
    >
      <div className="project-path-tooltip__row">
        <FolderIcon className="project-path-tooltip__icon" aria-hidden />
        <span className="project-path-tooltip__name" data-testid="multi-session-project-path-tooltip-name">{title}</span>
      </div>
      <div className="project-path-tooltip__divider" />
      <div className="project-path-tooltip__row project-path-tooltip__row--muted">
        <FolderIcon className="project-path-tooltip__icon project-path-tooltip__icon--muted" aria-hidden />
        <span className="project-path-tooltip__path" dir="ltr" data-testid="multi-session-project-path-tooltip-path">{path}</span>
      </div>
    </div>
  );
}

function PathInputDialog({
  title,
  initialValue = '',
  placeholder = '/Users/name/work/project',
  error,
  onCancel,
  onSubmit,
}: {
  title: string;
  initialValue?: string;
  placeholder?: string;
  error?: string | null;
  onCancel: () => void;
  onSubmit: (value: string) => void;
}) {
  const { t } = useTranslation();
  const [value, setValue] = useState(initialValue);

  return (
    <div className="conversation-path-dialog-backdrop" role="presentation" data-testid="multi-session-path-dialog-backdrop">
      <form
        className="conversation-path-dialog"
        data-testid="multi-session-path-dialog"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = value.trim();
          if (trimmed) onSubmit(trimmed);
        }}
      >
        <div className="conversation-path-dialog__title" data-testid="multi-session-path-dialog-title">{title}</div>
        <input
          className="conversation-path-dialog__input"
          data-testid="multi-session-path-dialog-input"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder={placeholder}
          maxLength={200}
          autoFocus
        />
        {error ? <div className="conversation-path-dialog__error" data-testid="multi-session-path-dialog-error">{error}</div> : null}
        <div className="conversation-path-dialog__actions" data-testid="multi-session-path-dialog-actions">
          <button type="button" onClick={onCancel} data-testid="multi-session-path-dialog-cancel">{t('multiSession.project.cancel')}</button>
          <button type="submit" disabled={!value.trim()} data-testid="multi-session-path-dialog-confirm">{t('multiSession.project.confirm')}</button>
        </div>
      </form>
    </div>
  );
}

function ProjectAddMenu({
  onCreateBlank,
  onSelectExisting,
}: {
  onCreateBlank: () => void;
  onSelectExisting: () => void;
}) {
  return (
    <div className="conversation-sidebar__add-menu" role="menu" data-testid="multi-session-project-add-menu">
      <ProjectCreateMenu
        onCreate={(mode) => {
          switch (mode) {
            case 'blank':
              onCreateBlank();
              break;
            case 'existing':
              onSelectExisting();
              break;
          }
        }}
        itemClassName="conversation-list-item__menu-item"
        blankIcon={<AddProjectIcon aria-hidden />}
        existingIcon={<FolderIcon aria-hidden />}
      />
    </div>
  );
}

function ProjectCreateDialog({
  mode,
  error,
  initial,
  onCancel,
  onSubmit,
}: {
  mode: 'blank' | 'existing';
  error?: string | null;
  initial?: { name?: string; path?: string } | null;
  onCancel: () => void;
  onSubmit: (name: string, projectDir: string) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState('');
  const [projectDir, setProjectDir] = useState('');
  const canSubmit = Boolean(name.trim() && (mode === 'blank' || projectDir.trim()));

  useEffect(() => {
    setName(initial?.name || '');
    setProjectDir(initial?.path || '');
  }, [initial]);

  return (
    <div className="conversation-path-dialog-backdrop" role="presentation" data-testid="multi-session-project-create-dialog-backdrop">
      <form
        className="conversation-path-dialog"
        data-testid="multi-session-project-create-dialog"
        onSubmit={(event) => {
          event.preventDefault();
          if (canSubmit) onSubmit(name.trim(), mode === 'blank' ? '' : projectDir.trim());
        }}
      >
        <button
          type="button"
          className="conversation-path-dialog__close"
          aria-label={t('common.close')}
          onClick={onCancel}
          data-testid="multi-session-project-create-dialog-close"
        >
          <CloseIcon aria-hidden />
        </button>
        <div className="conversation-path-dialog__title" data-testid="multi-session-project-create-dialog-title" data-variant={mode}>
          {mode === 'existing'
            ? t('multiSession.project.selectExisting')
            : t('multiSession.project.createBlank')}
        </div>
        <input
          className="conversation-path-dialog__input"
          data-testid="multi-session-project-create-dialog-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder={t('multiSession.project.namePlaceholder')}
          autoFocus
        />
        {mode === 'existing' ? (
          <input
            className="conversation-path-dialog__input"
            data-testid="multi-session-project-create-dialog-path"
            value={projectDir}
            onChange={(event) => setProjectDir(event.target.value)}
            placeholder={t('multiSession.project.pathPlaceholder')}
          />
        ) : null}
        {error ? <div className="conversation-path-dialog__error" data-testid="multi-session-project-create-dialog-error">{error}</div> : null}
        <div className="conversation-path-dialog__actions" data-testid="multi-session-project-create-dialog-actions">
          <button type="button" onClick={onCancel} data-testid="multi-session-project-create-dialog-cancel">{t('multiSession.project.cancel')}</button>
          <button type="submit" disabled={!canSubmit} data-testid="multi-session-project-create-dialog-confirm">{t('multiSession.project.confirm')}</button>
        </div>
      </form>
    </div>
  );
}

function ProjectDeleteDialog({
  project,
  action,
  error,
  deleting,
  onCancel,
  onDelete,
}: {
  project: ProjectInfo;
  action: 'delete' | 'archive';
  error?: string | null;
  deleting: boolean;
  onCancel: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  return (
    <DeleteDialog
      title={project.name}
      dialogTitle={t(`multiSession.project.${action === 'delete' ? 'deleteProject' : 'archiveSessions'}`)}
      confirmLabel={t(action === 'archive' ? 'multiSession.project.confirm' : 'common.delete')}
      descriptionKey={`multiSession.project.${action === 'delete' ? 'deleteProjectDescription' : 'archiveSessionsDescription'}`}
      descriptionValues={{ projectName: project.name }}
      deleting={deleting}
      error={error ?? null}
      onCancel={onCancel}
      onDelete={onDelete}
    />
  );
}

type RenameTarget =
  | { kind: 'project'; id: string; value: string }
  | { kind: 'session'; id: string; value: string };

function CronJobRow({
  job,
  cronExpanded,
  isCronUnread,
  onRowClick,
}: {
  job: SidebarCronJob;
  cronExpanded: boolean;
  isCronUnread: boolean;
  onRowClick: () => void;
}) {
  const rowRef = useRef<HTMLDivElement>(null);
  const { tooltip, handlers } = useAdaptiveTooltip({ anchorRef: rowRef, offsetY: 2, align: 'left' });
  const nameTooltipHandlers = {
    onMouseEnter: (event: React.MouseEvent<HTMLSpanElement>) => {
      const el = event.currentTarget;
      el.setAttribute('data-tooltip', el.scrollWidth > el.clientWidth ? job.name : '');
      handlers.onMouseEnter(event);
    },
    onMouseLeave: (event: React.MouseEvent<HTMLSpanElement>) => {
      event.currentTarget.setAttribute('data-tooltip', '');
      handlers.onMouseLeave();
    },
    onFocus: (event: React.FocusEvent<HTMLSpanElement>) => {
      const el = event.currentTarget;
      el.setAttribute('data-tooltip', el.scrollWidth > el.clientWidth ? job.name : '');
      handlers.onFocus(event);
    },
    onBlur: (event: React.FocusEvent<HTMLSpanElement>) => {
      event.currentTarget.setAttribute('data-tooltip', '');
      handlers.onBlur();
    },
  };
  return (
    <>
      <div
        ref={rowRef}
        className={`conversation-sidebar__cron-row conversation-sidebar__row-el${cronExpanded ? ' is-expanded' : ''}`}
        onClick={onRowClick}
        data-testid="multi-session-cron-job-row"
        data-variant={job.id}
      >
        <CronIcon className="conversation-sidebar__cron-row-icon" aria-hidden />
        <span className="conversation-sidebar__cron-row-name" data-tooltip="" data-testid="multi-session-cron-job-row-name" {...nameTooltipHandlers}>{job.name}</span>
        {isCronUnread && <span className="conversation-list-item__status-dot" aria-hidden="true" data-testid="multi-session-cron-job-row-unread" />}
        {cronExpanded ? <CollapseIcon className="conversation-sidebar__cron-row-chevron" aria-hidden /> : <ArrowRightIcon className="conversation-sidebar__cron-row-chevron" aria-hidden />}
      </div>
      {tooltip}
    </>
  );
}

export function ConversationSidebar({
  activeSessionId,
  onNew,
  onSelect,
  onOpenCron,
  isCronActive,
  collapsed = false,
  floating = false,
  onToggleCollapse,
}: ConversationSidebarProps) {
  const { t } = useTranslation();
  const { tooltip: conversationsTooltip, handlers: conversationsTooltipHandlers } = useAdaptiveTooltip();
  const runtimes = useChatStore((state) => state.runtimes);
  const [relativeTimeNow, setRelativeTimeNow] = useState(Date.now);
  const [unreadSessions, setUnreadSessions] = useState(loadUnreadSessions);
  const [pathDialogOpen, setPathDialogOpen] = useState(false);
  const [projectCreateMode, setProjectCreateMode] = useState<'blank' | 'existing'>('existing');
  const [pathDialogError, setPathDialogError] = useState<string | null>(null);
  const [pathDialogInitial, setPathDialogInitial] = useState<{ name?: string; path?: string } | null>(null);
  const [renameTarget, setRenameTarget] = useState<RenameTarget | null>(null);
  const [renameError, setRenameError] = useState<string | null>(null);
  const [pinError, setPinError] = useState<string | null>(null);
  // 既有「删除项目」流程状态：与归档并存，互不影响
  const [deleteProjectTarget, setDeleteProjectTarget] = useState<ProjectInfo | null>(null);
  const [projectAction, setProjectAction] = useState<'delete' | 'archive'>('delete');
  const [deleteProjectBusy, setDeleteProjectBusy] = useState(false);
  const [deleteProjectError, setDeleteProjectError] = useState<string | null>(null);
  const [projectAddMenuOpen, setProjectAddMenuOpen] = useState(false);
  const [workModeMenuOpen, setWorkModeMenuOpen] = useState(false);
  const addMenuRef = useRef<HTMLDivElement>(null);
  const workModeMenuRef = useRef<HTMLDivElement>(null);
  const previousProcessing = useRef<Record<string, boolean>>({});
  const archiveInFlightRef = useRef(new Set<string>());

  useEffect(() => {
    if (!pathDialogError || pathDialogOpen) return;
    const timeoutId = window.setTimeout(() => setPathDialogError(null), 3000);
    return () => window.clearTimeout(timeoutId);
  }, [pathDialogError, pathDialogOpen]);

  const {
    workMode,
    projects,
    projectSessions,
    projectSessionTotals,
    sessionVisibility,
    pinnedSessions,
    expandedProjectIds,
    setSelectedProject,
    toggleProjectExpanded,
    createProject,
    renameProject,
    pinProject,
    removeProject,
    archiveSession,
    removeSessionLocally,
    loadProjectSessions,
    showMoreSessions,
    collapseSessions,
    pinSession,
    renameSession,
    setWorkMode,
  } = useWorkspaceStore();

  useEffect(() => {
    if (!workModeMenuOpen) return;
    const close = (event: MouseEvent) => {
      if (!workModeMenuRef.current?.contains(event.target as Node)) setWorkModeMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setWorkModeMenuOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [workModeMenuOpen]);

  const switchWorkMode = async (nextMode: 'work' | 'code') => {
    setWorkModeMenuOpen(false);
    if (nextMode === workMode) return;
    await setWorkMode(nextMode);
    onNew();
  };

  const cronJobs = useCronStore((s) => s.jobs);
  const loadCronJobs = useCronStore((s) => s.loadJobs);
  const expandedCronGroups = useCronStore((s) => s.expandedCronGroups);
  const toggleCronGroup = useCronStore((s) => s.toggleCronGroup);
  const cronSessions = useCronStore((s) => s.cronSessions);
  const cronSessionsLoading = useCronStore((s) => s.cronSessionsLoading);
  const loadCronSessions = useCronStore((s) => s.loadCronSessions);
  const unreadCronJobs = useCronStore((s) => s.unreadCronJobs);
  const clearCronJobUnread = useCronStore((s) => s.clearCronJobUnread);

  useEffect(() => {
    void loadCronJobs();
  }, [loadCronJobs]);

  // 监听 agent 工具调用结果：当 cron 相关工具执行后刷新侧边栏定时任务
  useEffect(() => {
    const CRON_TOOL_PREFIX = 'cron_';
    const unsubscribe = webClient.on('chat.tool_result', (event) => {
      const payload = event.payload as Record<string, unknown>;
      const inner = (payload?.tool_result as Record<string, unknown>) ?? payload;
      const toolName = String(inner?.tool_name ?? inner?.name ?? '');
      if (toolName === 'cron' || toolName.startsWith(CRON_TOOL_PREFIX)) {
        void loadCronJobs();
      }
    });
    return unsubscribe;
  }, [loadCronJobs]);

  // 按项目归属定时任务
  const jobsByProject = useMemo(() => {
    const map = new Map<string, SidebarCronJob[]>();
    for (const project of projects) {
      const jobs = filterJobsForProject(cronJobs, project.project_id);
      if (jobs.length > 0) map.set(project.project_id, jobs);
    }
    return map;
  }, [cronJobs, projects]);

  useEffect(() => {
    const timer = window.setInterval(() => setRelativeTimeNow(Date.now()), RELATIVE_TIME_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!activeSessionId) return;
    setUnreadSessions((current) => {
      if (!current.has(activeSessionId)) return current;
      const next = new Set(current);
      next.delete(activeSessionId);
      return next;
    });
  }, [activeSessionId]);

  useEffect(() => {
    localStorage.setItem(UNREAD_KEY, JSON.stringify([...unreadSessions]));
  }, [unreadSessions]);

  useEffect(() => {
    if (!projectAddMenuOpen) return;
    const close = (event: MouseEvent) => {
      if (!addMenuRef.current?.contains(event.target as Node)) setProjectAddMenuOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setProjectAddMenuOpen(false);
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [projectAddMenuOpen]);

  const projectIdSnapshot = useMemo(() => projects.map((project) => project.project_id).join('\0'), [projects]);

  useEffect(() => {
    for (const projectId of projectIdSnapshot.split('\0')) {
      if (projectId && expandedProjectIds[projectId]) {
        void loadProjectSessions(projectId);
      }
    }
  }, [expandedProjectIds, loadProjectSessions, projectIdSnapshot]);

  const projectByDir = useMemo(() => {
    const byDir = new Map<string, ProjectInfo>();
    for (const project of projects) {
      if (project.project_dir) byDir.set(project.project_dir, project);
    }
    return byDir;
  }, [projects]);
  const defaultProject = useMemo(
    () => projects.find(isDefaultProject),
    [projects],
  );

  const pinnedProjects = useMemo(() => projects.filter((project) => project.pinned && !isDefaultProject(project)), [projects]);
  const regularProjects = useMemo(() => projects.filter((project) => !project.pinned && !isDefaultProject(project)), [projects]);
  const sortedProjectSessions = useMemo(() => {
    const sorted: Record<string, Session[]> = {};
    for (const [projectId, list] of Object.entries(projectSessions)) {
      sorted[projectId] = sortSessionsForSidebar(list);
    }
    return sorted;
  }, [projectSessions]);
  const conversationSessions = useMemo(() => {
    if (defaultProject) return sortedProjectSessions[defaultProject.project_id] || [];
    return [];
  }, [defaultProject, sortedProjectSessions]);
  const orderedPinnedSessions = useMemo(() => sortSessionsForSidebar(pinnedSessions), [pinnedSessions]);
  const observedSidebarSessions = useMemo(() => {
    const byId = new Map<string, Session>();
    for (const session of orderedPinnedSessions) {
      byId.set(session.session_id, session);
    }
    for (const list of Object.values(sortedProjectSessions)) {
      for (const session of list) {
        byId.set(session.session_id, session);
      }
    }
    return Array.from(byId.values());
  }, [orderedPinnedSessions, sortedProjectSessions]);

  useEffect(() => {
    const { snapshot, completedInBackground } = getProcessingTransitions(
      previousProcessing.current,
      observedSidebarSessions,
      runtimes,
      activeSessionId,
    );
    previousProcessing.current = snapshot;
    if (completedInBackground.length > 0) {
      setUnreadSessions((current) => new Set([...current, ...completedInBackground]));
    }
  }, [activeSessionId, observedSidebarSessions, runtimes]);

  async function handlePinSession(session: Session) {
    setPinError(null);
    try {
      await pinSession(session.session_id, !session.pinned);
    } catch (error) {
      setPinError(error instanceof Error ? error.message : String(error));
    } finally {
      // 置顶/取消置顶后刷新所有展开的定时任务触发列表，保证触发会话实时回归/移除
      for (const [groupId, isOpen] of Object.entries(expandedCronGroups)) {
        if (!isOpen) continue;
        const cronId = groupId.startsWith('cron-') ? groupId.slice(5) : groupId;
        const job = cronJobs.find((j) => j.id === cronId);
        if (!job) continue;
        void loadCronSessions(job.project_id || 'default', cronId);
      }
    }
  }

  async function handleRenameSession(sessionId: string, title: string) {
    await renameSession(sessionId, title);
    // 重命名后刷新所有展开的定时任务触发列表，保证标题立即更新
    for (const [groupId, isOpen] of Object.entries(expandedCronGroups)) {
      if (!isOpen) continue;
      const cronId = groupId.startsWith('cron-') ? groupId.slice(5) : groupId;
      const job = cronJobs.find((j) => j.id === cronId);
      if (!job) continue;
      void loadCronSessions(job.project_id || 'default', cronId);
    }
  }

  async function handleRenameSubmit(value: string) {
    if (!renameTarget) return;
    setRenameError(null);
    setPinError(null);
    try {
      if (renameTarget.kind === 'project') {
        await renameProject(renameTarget.id, value);
      } else {
        await handleRenameSession(renameTarget.id, value);
      }
      setRenameTarget(null);
    } catch (error) {
      setRenameError(error instanceof Error ? error.message : String(error));
    }
  }

  async function handleCreateProject(name: string, projectDir: string) {
    setPathDialogError(null);
    if (projectDir && (!isLikelyAbsolutePath(projectDir) || projectDir.startsWith('~/'))) {
      setPathDialogError(t('multiSession.project.absolutePathError'));
      return;
    }
    try {
      await createProject(name, projectDir);
      setPathDialogInitial(null);
      setPathDialogOpen(false);
    } catch (error) {
      const errorKey = projectCreateErrorKey(error);
      setPathDialogError(errorKey ? t(errorKey) : error instanceof Error ? error.message : String(error));
    }
  }

  async function handleSelectExistingProjectDirectory() {
    setPathDialogError(null);
    if (!isProjectDirectoryPickerSupported()) {
      setProjectCreateMode('existing');
      setPathDialogInitial(null);
      setPathDialogOpen(true);
      return;
    }

    const result = await selectProjectDirectory();
    if (!result.ok) {
      if (result.reason === 'cancelled') return;
      setProjectCreateMode('existing');
      setPathDialogInitial(null);
      setPathDialogOpen(true);
      setPathDialogError(
        result.reason === 'unsupported'
          ? t('multiSession.project.directoryPickerUnsupported')
          : result.message || t('multiSession.project.directoryPickerFailed'),
      );
      return;
    }
    try {
      await createProject(result.name, result.path);
      setPathDialogInitial(null);
      setPathDialogOpen(false);
    } catch (error) {
      const errorKey = projectCreateErrorKey(error);
      setPathDialogError(errorKey ? t(errorKey) : error instanceof Error ? error.message : String(error));
    }
  }

  // 归档错误码只用于分支判断，用户看到的是可翻译文案
  function archiveErrorKey(error: unknown): string {
    const code = getArchiveErrorCode(error);
    if (code === 'SESSION_BUSY') return 'multiSession.project.errors.archiveSessionBusy';
    if (code === 'FORBIDDEN') return 'multiSession.project.errors.archiveForbidden';
    if (code === 'NOT_FOUND') return 'multiSession.project.errors.archiveNotFound';
    return 'multiSession.project.errors.archiveFailed';
  }

  function openArchiveSuccessToast(options: {
    content: string;
    onUndo: () => Promise<void>;
  }) {
    // 新的归档提示替换旧提示，避免多个「撤销」并发把列表刷乱
    toast.closeAll();
    toast.open({
      content: options.content,
      icon: <Archive aria-hidden size={16} strokeWidth={1.8} />,
      duration: 5,
      actions: [
        {
          label: t('multiSession.project.archiveView'),
          onClick: () => requestSettingsModule('archivedTasks'),
        },
        {
          label: t('multiSession.project.archiveUndo'),
          onClick: () => {
            void (async () => {
              try {
                await options.onUndo();
              } catch (error) {
                toast.open({ content: t(archiveErrorKey(error)), variant: 'error' });
              }
            })();
          },
        },
      ],
    });
  }

  async function handleArchiveSession(session: Session) {
    const opKey = `session:${session.session_id}`;
    if (archiveInFlightRef.current.has(opKey)) return;
    archiveInFlightRef.current.add(opKey);
    try {
      await archiveSession(session.session_id);
      flushSync(() => {
        removeSessionLocally(session.session_id);
        openArchiveSuccessToast({
          content: t('multiSession.project.conversationArchived'),
          onUndo: async () => {
            const response = await archivedTaskClient.unarchiveSession(session.session_id);
            const entry = findBatchSessionResult(response, session.session_id);
            if (!entry?.ok) {
              const error = new Error(entry?.error || 'Failed to unarchive session');
              Object.assign(error, { code: entry?.code });
              throw error;
            }
            await useWorkspaceStore.getState().refreshWorkspaceData();
          },
        });
      });
    } catch (error) {
      toast.open({ content: t(archiveErrorKey(error)), variant: 'error' });
      await useWorkspaceStore.getState().refreshWorkspaceData();
    } finally {
      archiveInFlightRef.current.delete(opKey);
    }
  }

  async function handleRemoveProject() {
    if (!deleteProjectTarget || (projectAction === 'delete' && isDefaultProject(deleteProjectTarget))) return;
    setDeleteProjectBusy(true);
    setDeleteProjectError(null);
    try {
      const projectId = deleteProjectTarget.project_id;
      if (projectAction === 'delete') {
        await removeProject(projectId);
        await loadCronJobs();
      } else {
        const result = await projectRegistryClient.archiveSessions(projectId);
        const succeededIds = result.results.filter((item) => item.ok).map((item) => item.session_id);
        const workspace = useWorkspaceStore.getState();
        workspace.removeSessions(succeededIds);
        await Promise.all([workspace.loadProjects(), workspace.loadProjectSessions(projectId), workspace.loadPinnedSessions()]);
        if (result.failed_count) {
          setDeleteProjectError(t('multiSession.project.batchPartialFailure', { succeeded: result.succeeded_count, failed: result.failed_count })
            + ' ' + result.results.filter((item) => !item.ok).map((item) => `${item.session_id}: ${item.error || item.code}`).join('; '));
          return;
        }
        // 批量归档成功：复用单会话归档的 toast（查看归档页 + 撤销）。
        // 撤销走批量恢复接口，把本次成功归档的会话原样恢复回活跃区。
        openArchiveSuccessToast({
          content: t('multiSession.project.sessionsArchived', { count: succeededIds.length }),
          onUndo: async () => {
            const response = await archivedTaskClient.unarchiveSessions(succeededIds);
            const failed = response.results.filter((item) => !item.ok);
            if (failed.length) {
              const error = new Error(failed[0]?.error || 'Failed to unarchive sessions');
              Object.assign(error, { code: failed[0]?.code });
              throw error;
            }
            await useWorkspaceStore.getState().refreshWorkspaceData();
          },
        });
      }
      setDeleteProjectTarget(null);
    } catch (error) {
      // project.delete 的部分失败 payload 带有首个失败项的可读错误，优先展示
      const partial = parseProjectOperationFailure(error);
      setDeleteProjectError(partial?.detail ?? (error instanceof Error ? error.message : String(error)));
    } finally {
      setDeleteProjectBusy(false);
    }
  }

  function renderSession(session: Session, options: { nested?: boolean; projectMenu?: boolean } = {}) {
    const nested = options.nested === true;
    const projectMenu = options.projectMenu === true;
    return (
      <ConversationListItem
        key={session.session_id}
        session={session}
        runtime={runtimes[session.session_id]}
        active={activeSessionId === session.session_id}
        nested={nested}
        unread={unreadSessions.has(session.session_id)}
        now={relativeTimeNow}
        onSelect={() => onSelect(session)}
        onPin={() => void handlePinSession(session)}
        onArchive={() => void handleArchiveSession(session)}
        menuItems={projectMenu
          ? getProjectSessionMenuItems(Boolean(session.pinned), t)
          : getConversationMenuItems(Boolean(session.pinned), t)}
        onRename={() => setRenameTarget({
          kind: 'session',
          id: session.session_id,
          value: getSessionTitle(session, t('multiSession.untitled')),
        })}
      />
    );
  }

  function renderCronJob(job: SidebarCronJob, projectId: string, nested = false) {
    const cronGroupId = `cron-${job.id}`;
    const cronExpanded = expandedCronGroups[cronGroupId] ?? false;
    const triggerSessions = cronSessions[job.id] || [];
    const isCronSessionsLoading = cronSessionsLoading[job.id] ?? false;
    const isCronUnread = Boolean(unreadCronJobs[job.id]);
    return (
      <div key={`cron-wrapper-${job.id}`} className={`conversation-sidebar__session-wrapper${nested ? ' conversation-sidebar__session-wrapper--nested' : ''}`}>
        <CronJobRow
          job={job}
          cronExpanded={cronExpanded}
          isCronUnread={isCronUnread}
          onRowClick={() => {
            toggleCronGroup(cronGroupId);
            if (isCronUnread) clearCronJobUnread(job.id);
            if (!cronExpanded) {
              void loadCronSessions(projectId, job.id);
            }
          }}
        />
        {cronExpanded ? (
          <div className="conversation-sidebar__cron-sessions" data-testid="multi-session-cron-job-sessions">
            {isCronSessionsLoading ? (
              <div className="conversation-sidebar__cron-sessions-loading" data-testid="multi-session-cron-job-sessions-loading">{t('common.loading')}</div>
            ) : triggerSessions.length > 0 ? (
              triggerSessions.map((ts) => (
                <ConversationListItem
                  key={ts.session_id}
                  session={ts}
                  runtime={runtimes[ts.session_id]}
                  active={activeSessionId === ts.session_id}
                  nested={false}
                  unread={unreadSessions.has(ts.session_id)}
                  now={relativeTimeNow}
                  onSelect={() => {
                    clearCronJobUnread(job.id);
                    onSelect(ts);
                  }}
                  onPin={() => void handlePinSession(ts)}
                  onArchive={() => void handleArchiveSession(ts)}
                  menuItems={getConversationMenuItems(Boolean(ts.pinned), t, { archivable: false })}
                  onRename={() => setRenameTarget({
                    kind: 'session',
                    id: ts.session_id,
                    value: getSessionTitle(ts, t('multiSession.untitled')),
                  })}
                />
              ))
            ) : (
              <div className="conversation-sidebar__cron-sessions-empty" data-testid="multi-session-cron-job-sessions-empty">{t('multiSession.project.noSessions')}</div>
            )}
          </div>
        ) : null}
      </div>
    );
  }

  function getSessionProject(session: Session): ProjectInfo | undefined {
    if (session.project_dir === '') return defaultProject;
    return session.project_dir ? projectByDir.get(session.project_dir) : undefined;
  }

  function renderSessionPagination(projectId: string, nested = false) {
    const visibleCount = sessionVisibility[projectId]?.visibleCount ?? PROJECT_SESSION_PAGE_SIZE;
    const renderedCount = projectSessions[projectId]?.length ?? 0;
    const total = projectSessionTotals[projectId] ?? projectSessions[projectId]?.length ?? 0;
    const canShowMore = total > renderedCount;
    const canCollapse = visibleCount > PROJECT_SESSION_PAGE_SIZE;

    if (!canShowMore && !canCollapse) return null;

    return (
      <div className={`conversation-sidebar__pagination${nested ? ' conversation-sidebar__pagination--nested' : ''}`} data-testid="multi-session-pagination">
        {canShowMore ? (
          <button
            type="button"
            className="conversation-sidebar__pagination-button"
            onClick={() => { void showMoreSessions(projectId); }}
            data-testid="multi-session-pagination-show-more"
          >
            {t('multiSession.showMore')}
          </button>
        ) : null}
        {canCollapse ? (
          <button
            type="button"
            className="conversation-sidebar__pagination-button"
            onClick={() => { void collapseSessions(projectId); }}
            data-testid="multi-session-pagination-collapse"
          >
            {t('multiSession.collapse')}
          </button>
        ) : null}
      </div>
    );
  }

  function renderProject(project: ProjectInfo) {
    const sessionsForProject = sortedProjectSessions[project.project_id] || [];
    const expanded = Boolean(expandedProjectIds[project.project_id]);
    const hasUnreadCronResult = (jobsByProject.get(project.project_id) || []).some(
      (job) => Boolean(unreadCronJobs[job.id]),
    );
    return (
      <div key={project.project_id} className="conversation-sidebar__group" data-testid="multi-session-project-group" data-variant={project.project_id}>
        <ProjectEntityRow
          title={project.name}
          path={project.project_dir || undefined}
          isExpanded={expanded}
          isPinned={project.pinned}
          hasUnreadCronResult={hasUnreadCronResult}
          defaultProject={isDefaultProject(project)}
          newLabel={getProjectNewLabel(project.name, t)}
          projectId={project.project_id}
          onToggle={() => toggleProjectExpanded(project.project_id)}
          onNew={() => {
            setSelectedProject(project);
            onNew({ preserveProject: true, project });
          }}
          onPin={() => {
            if (isDefaultProject(project)) return;
            void pinProject(project.project_id, !project.pinned);
          }}
          onRename={() => {
            if (isDefaultProject(project)) return;
            setRenameError(null);
            setRenameTarget({ kind: 'project', id: project.project_id, value: project.name });
          }}
          onRemove={() => {
            if (isDefaultProject(project)) return;
            setProjectAction('delete');
            setDeleteProjectError(null);
            setDeleteProjectTarget(project);
          }}
          onBatch={(action) => {
            setProjectAction(action);
            setDeleteProjectError(null);
            setDeleteProjectTarget(project);
          }}
        />
        {expanded ? (
          <div className="conversation-sidebar__group-list" data-testid="multi-session-project-group-list">
            {(jobsByProject.get(project.project_id) || []).map((job) => renderCronJob(job, project.project_id, true))}
            {sessionsForProject.length > 0 ? sessionsForProject.map((session) => renderSession(session, { nested: true, projectMenu: true })) : (
              (jobsByProject.get(project.project_id) || []).length === 0 ? <div className="conversation-sidebar__empty" data-testid="multi-session-project-group-empty">{t('multiSession.project.noConversations')}</div> : null
            )}
            {renderSessionPagination(project.project_id, true)}
          </div>
        ) : null}
      </div>
    );
  }

  const hasPinnedSection = pinnedProjects.length > 0 || orderedPinnedSessions.length > 0;

  const showOverlay = floating && !collapsed;

  return (
    <>
    {showOverlay && (
      <div className="conversation-sidebar__overlay" data-testid="multi-session-sidebar-overlay" onClick={onToggleCollapse} />
    )}
    <aside className={`conversation-sidebar${floating ? ' is-floating' : ''}${collapsed ? ' is-collapsed' : ''}`} aria-label={t('multiSession.conversations')} data-testid="multi-session-sidebar">
      <div className="conversation-sidebar__inner">
        <div ref={workModeMenuRef} className="conversation-sidebar__mode" data-testid="multi-session-work-mode">
        <button
          type="button"
          className="conversation-sidebar__mode-trigger"
          onClick={() => setWorkModeMenuOpen((open) => !open)}
          aria-haspopup="menu"
          aria-expanded={workModeMenuOpen}
          data-testid="multi-session-work-mode-trigger"
        >
          <span data-testid="multi-session-work-mode-label" data-variant={workMode}>{workMode === 'code' ? t('codeMode.code') : t('codeMode.work')}</span>
          <ChevronDown size={15} className={workModeMenuOpen ? 'is-open' : ''} />
        </button>
        {workModeMenuOpen ? (
          <div className="conversation-sidebar__mode-menu" role="menu" data-testid="multi-session-work-mode-menu">
            <button
              type="button"
              className={workMode === 'work' ? 'is-active' : ''}
              onClick={() => void switchWorkMode('work')}
              role="menuitemradio"
              aria-checked={workMode === 'work'}
              data-testid="multi-session-work-mode-menu-work"
            >
              <Workflow size={17} />
              <span>
                <strong>{t('codeMode.work')}</strong>
                <small>{t('codeMode.workDescription')}</small>
              </span>
              {workMode === 'work' ? <Check size={16} /> : null}
            </button>
            <button
              type="button"
              className={workMode === 'code' ? 'is-active' : ''}
              onClick={() => void switchWorkMode('code')}
              role="menuitemradio"
              aria-checked={workMode === 'code'}
              data-testid="multi-session-work-mode-menu-code"
            >
              <Code2 size={17} />
              <span>
                <strong>{t('codeMode.code')}</strong>
                <small>{t('codeMode.codeDescription')}</small>
              </span>
              {workMode === 'code' ? <Check size={16} /> : null}
            </button>
          </div>
        ) : null}
        <button
          type="button"
          className="conversation-sidebar__mode-collapse"
          onClick={onToggleCollapse}
          aria-label={t('common.collapse') || 'Collapse'}
          data-testid="multi-session-sidebar-collapse"
        >
          <PanelCollapseIcon aria-hidden />
        </button>
        </div>
        <div className="conversation-sidebar__operations" data-testid="multi-session-operations">
        <button type="button" className="conversation-sidebar__new" onClick={() => {
          setSelectedProject(null);
          setPinError(null);
          onNew();
        }}
        data-testid="multi-session-new-conversation-button">
          <NewTaskIcon aria-hidden />
          <span data-testid="multi-session-new-conversation-label">{t('multiSession.newConversation')}</span>
        </button>
        <button
          type="button"
          className={`conversation-sidebar__new${isCronActive ? ' is-active' : ''}`}
          onClick={onOpenCron}
          data-testid="multi-session-open-cron-button"
        >
          <CronIcon aria-hidden />
          <span data-testid="multi-session-open-cron-label">{t('nav.cron')}</span>
        </button>
        </div>
        <div className="conversation-sidebar__body" data-testid="multi-session-sidebar-body">
        {hasPinnedSection ? (
          <div className="conversation-sidebar__group conversation-sidebar__group--pinned" data-testid="multi-session-pinned-group">
            <div className="conversation-sidebar__section-heading" data-testid="multi-session-pinned-group-heading">
              <span className="conversation-sidebar__label" data-testid="multi-session-pinned-group-label">{t('multiSession.project.pinned')}</span>
            </div>
            <div className="conversation-sidebar__group-list" data-testid="multi-session-pinned-group-list">
              {orderedPinnedSessions.map((session) => {
                const project = getSessionProject(session);
                return renderSession(session, {
                  projectMenu: Boolean(project && !isDefaultProject(project)),
                });
              })}
              {pinnedProjects.map((project) => renderProject(project))}
            </div>
          </div>
        ) : null}
        {pinError ? (
          <div className="conversation-sidebar__error" role="alert" data-testid="multi-session-pin-error">
            {t('multiSession.project.pinFailed')}: {pinError}
          </div>
        ) : null}
        {pathDialogError && !pathDialogOpen ? (
          <div className="app-toast-wrapper app-toast-wrapper--top-center" data-testid="multi-session-path-error-toast">
            <div className="app-session-toast" role="status" aria-live="polite" data-testid="multi-session-path-error-toast-message">
              {pathDialogError}
            </div>
          </div>
        ) : null}
        <div className="conversation-sidebar__group conversation-sidebar__project-add" ref={addMenuRef} data-testid="multi-session-project-add-group">
          <div className="conversation-sidebar__section-heading" data-testid="multi-session-project-add-heading">
            <span className="conversation-sidebar__label" data-testid="multi-session-project-add-label">{t('multiSession.project.projects')}</span>
            <div className="conversation-sidebar__section-actions">
            <button
              type="button"
              className="conversation-sidebar__section-action"
              onClick={() => {
                setProjectAddMenuOpen((open) => !open);
              }}
              title={t('multiSession.project.newProject')}
              aria-label={t('multiSession.project.newProject')}
              aria-haspopup="menu"
              aria-expanded={projectAddMenuOpen}
              data-testid="multi-session-new-project-button"
            >
              <PlusIcon aria-hidden />
            </button>
            </div>
          </div>
          {projectAddMenuOpen ? (
            <ProjectAddMenu
              onCreateBlank={() => {
                setProjectAddMenuOpen(false);
                setProjectCreateMode('blank');
                setPathDialogError(null);
                setPathDialogInitial(null);
                setPathDialogOpen(true);
              }}
              onSelectExisting={() => {
                setProjectAddMenuOpen(false);
                void handleSelectExistingProjectDirectory();
              }}
            />
          ) : null}
          <div className="conversation-sidebar__group-list" data-testid="multi-session-project-add-list">
            {regularProjects.length === 0 ? (
              <div className="conversation-sidebar__empty" data-testid="multi-session-project-add-empty">{t('multiSession.project.noProjects')}</div>
            ) : null}
            {regularProjects.map((project) => renderProject(project))}
          </div>
        </div>
        <div className="conversation-sidebar__group conversation-sidebar__group--conversations" data-testid="multi-session-conversations-group">
          <div className="conversation-sidebar__section-heading" data-testid="multi-session-conversations-heading">
            <span className="conversation-sidebar__label" data-testid="multi-session-conversations-label">{t('multiSession.conversations')}</span>
            <button
              type="button"
              className="conversation-sidebar__section-new"
              onClick={() => {
                setSelectedProject(null);
                setPinError(null);
                onNew();
              }}
              aria-label={t('multiSession.project.newConversation')}
              data-tooltip={t('multiSession.project.newConversation')}
              data-testid="multi-session-conversations-new-button"
              {...conversationsTooltipHandlers}
            >
              <PlusIcon aria-hidden />
            </button>
          </div>
          <div className="conversation-sidebar__group-list" data-testid="multi-session-conversations-list">
            {defaultProject ? (jobsByProject.get(defaultProject.project_id) || []).map((job) => renderCronJob(job, defaultProject.project_id)) : null}
            {conversationSessions.length > 0 ? conversationSessions.map((session) => renderSession(session)) : (
              (!defaultProject || (jobsByProject.get(defaultProject.project_id) || []).length === 0) ? <div className="conversation-sidebar__empty" data-testid="multi-session-conversations-empty">{t('multiSession.project.noConversations')}</div> : null
            )}
            {defaultProject ? renderSessionPagination(defaultProject.project_id, false) : null}
          </div>
        </div>
        </div>
      </div>
      {pathDialogOpen ? (
        <ProjectCreateDialog
          mode={projectCreateMode}
          error={pathDialogError}
          initial={pathDialogInitial}
          onCancel={() => {
            setPathDialogError(null);
            setPathDialogInitial(null);
            setPathDialogOpen(false);
          }}
          onSubmit={(name, projectDir) => void handleCreateProject(name, projectDir)}
        />
      ) : null}
      {renameTarget ? (
        <PathInputDialog
          title={t('multiSession.project.rename')}
          initialValue={renameTarget.value}
          placeholder={t('multiSession.project.renamePlaceholder')}
          error={renameError}
          onCancel={() => {
            setRenameError(null);
            setPinError(null);
            setRenameTarget(null);
          }}
          onSubmit={(value) => void handleRenameSubmit(value)}
        />
      ) : null}
      {deleteProjectTarget ? (
        <ProjectDeleteDialog
          project={deleteProjectTarget}
          action={projectAction}
          deleting={deleteProjectBusy}
          error={deleteProjectError}
          onCancel={() => {
            if (deleteProjectBusy) return;
            setDeleteProjectError(null);
            setDeleteProjectTarget(null);
          }}
          onDelete={() => { void handleRemoveProject(); }}
        />
      ) : null}
      {conversationsTooltip}
    </aside>
    </>
  );
}
