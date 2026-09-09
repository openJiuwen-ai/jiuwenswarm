import {
  useState,
  useRef,
  useCallback,
  KeyboardEvent,
  useEffect,
  ClipboardEvent,
  DragEvent,
  ChangeEvent,
  useMemo,
  forwardRef,
  useImperativeHandle,
  FormEvent,
  Fragment,
  type CSSProperties,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import type { TFunction } from 'i18next';
import { AtSign, ChevronRight, CircleX, Loader2, Plus, Settings, Square, Workflow, X } from 'lucide-react';
import { useSpeechRecognition } from '../../hooks';

// import { stopAllTts } from '../../utils';
import {
  useChatStore,
  useGoalStore,
  usePlanStore,
  useSessionStore,
  useWorkspaceStore,
} from '../../stores';
import { supportsPlanMode } from '../../features/planMode/wireMode';
import { applyPlanToggle, evaluatePlanToggle } from '../../features/planMode/planModeGate';
import { queueOrAddGoalObjectiveMessage } from '../../features/goalPendingObjectiveBubble';
import { AgentMode, MediaItem, Permission, type ProjectInfo } from '../../types';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import { ProjectCreateMenu, type ProjectCreateMode } from '../../multi-session/sidebar/ProjectCreateMenu';
import { projectCreateErrorKey } from '../../multi-session/sidebar/projectCreateErrors';
import { AGENT_MODE_OPTIONS, PERMISSION_OPTIONS } from '../../config/chatConfig';
import clsx from 'clsx';
import { PermissionWarningDialog } from './PermissionWarningDialog';
import ChatModelSelector from './ChatModelSelector';
import { FileIcon } from '../FileIcon';
import { getEvolutionPillLabel } from './evolution-status';
import { webRequest } from '../../services/webClient';
import {
  parseSlashLine,
  findSlashCommand,
  type SlashCommand,
  type SlashCommandContext,
} from './slashCommands/registry';
import {
  getWebSlashCommandsForMode,
  hasUnfinishedGoal as isUnfinishedGoal,
  isSlashCommandDisabledByGoal,
  shouldExecuteRegisteredSlashCommand,
  supportsWebSlashCommands,
} from './slashCommands/semantics';
import { withUploadDocumentBlock } from '../../utils/documentMessage';
import { ExtensionPickerPanel } from './ExtensionPickerPanel';
import { SkillPickerPanel } from './SkillPickerPanel';
import { PickerPanel } from './PickerPanel';
import { Switch } from '../Switch';
import { Input } from '../ui';
import { Select } from '../ui/Select/Select';
import { ExtensionIcon } from '../ConnectorMarket/icons';
import {
  isLikelyAbsolutePath,
  isProjectDirectoryPickerSupported,
  selectProjectDirectory,
} from '../../features/workspace/projectDirectoryPicker';
import {
  getClipboardFilePicks,
  isDesktopLocalFilePicker,
  isDesktopShell,
  selectLocalFiles,
  type LocalFilePick,
} from '../../features/workspace/localFilePicker';
import { useDesktopLocalFilePickerReady } from '../../hooks';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { getInputProjectOptions, isDefaultInputProject } from './projectSelection';
import {
  getClipboardImageFiles,
  IMAGE_INPUT_DISABLED_ALERT_KEY,
  isImageInputDisabled,
  shouldAlertImagePasteDisabled,
} from './clipboardImagePaste';
import AgentPickerIcon from '../../assets/agent-management/智能体选择.svg?react';
import AttachmentIcon from '../../assets/agent-management/attachment.svg?react';
import GoalIcon from '../../assets/agent-management/goal.svg?react';
import PlanIcon from '../../assets/agent-management/planned-events.svg?react';
import SearchIcon from '../../assets/agent-management/agent-search.svg?react';
import SkillIcon from '../../assets/agent-management/agent-skill.svg?react';

const MENU_GAP = 10;
/** 智能体选择列表单行高度（与 ChatPanel.css 的 .chat-agent-picker__item min-height 一致） */
const AGENT_PICKER_ROW_HEIGHT = 40;

function resolveMenuDirection(anchorBottom: number, menuHeight: number) {
  const spaceBelow = window.innerHeight - anchorBottom - MENU_GAP;
  if (spaceBelow >= menuHeight) return 'down' as const;
  return 'up' as const;
}
import sendIcon from '../../assets/send.svg';
import sendActiveIcon from '../../assets/send_active.svg';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { CodeBranchSelector } from '../../features/code-mode/CodeBranchSelector';
import { generateUuidV4 } from '../../utils/uuid';
import { buildInstalledSkillNames, filterEnabledMySkills } from '../../utils/mySkills';
import { createAgentManagementClient, getAgentAvatarUrl, type AgentCatalogItem } from '../../features/agentManagement';
import { ContextUsageIndicator } from './ContextUsageIndicator';
import { isImeCompositionKey } from './imeComposition';

/** 输入栏下拉所需的最小技能数据结构（与 SkillPanel 中的 SkillItem 保持一致） */
type InputAreaSkillItem = {
  name: string;
  /** 展示名（保留安装来源的原始大小写，如 ClawHub 的 Weather）；缺省回退到 name */
  display_name?: string;
  description: string;
  source: string;
  is_builtin?: boolean;
  is_builtin_source?: boolean;
  enabled?: boolean;
  installed?: boolean;
  tags?: string[];
  skill_type?: 'skill' | 'swarm_skill' | 'multimodal_skill';
};

type SlashCommandMeta = {
  name: string;
  description: string;
  usage?: string;
  takesArgs?: boolean;
  execution?: string;
  req_method?: string;
  mode?: string;
  plan_entry_source?: string;
  requires_session?: boolean;
};

/** 已安装插件信息（用于判定技能是否已安装） */
type InputAreaInstalledPlugin = {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit?: string | null;
  skills: Array<string | { name: string; version?: string | null }>;
};

type InputAreaTeamMember = {
  member_id: string;
  name?: string;
  status?: string;
};

type ComposerSuggestionKind = 'member' | 'role' | 'slash';
type WorkIconName = 'add' | 'arrow' | 'check' | 'close' | 'collapse' | 'expand' | 'folder' | 'search';

type ComposerSuggestionState = {
  kind: ComposerSuggestionKind;
  query: string;
};

type ComposerSuggestionItem = {
  id: string;
  label: string;
  status?: string;
  description?: string;
  itemKind?: 'command' | 'skill';
  takesArgs?: boolean;
  disabled?: boolean;
  disabledReason?: string;
};

function getComposerSuggestionItems(
  suggestion: ComposerSuggestionState | null,
  members: ComposerSuggestionItem[],
  slashCommands: SlashCommandMeta[],
  slashSkills: InputAreaSkillItem[],
  isTeamMode: boolean,
): ComposerSuggestionItem[] {
  if (!suggestion) return [];
  if (suggestion.kind === 'slash') {
    const query = suggestion.query.trim().toLowerCase();
    const commands = slashCommands
      .filter((command) => !query || command.name.toLowerCase().includes(query))
      .map((command) => ({
        id: command.name,
        label: `/${command.name}`,
        description: command.description,
        itemKind: 'command' as const,
        takesArgs: command.takesArgs,
      }));
    const skills = slashSkills
      .filter((skill) => (
        isTeamMode
          ? skill.skill_type === 'swarm_skill'
          : !skill.skill_type || skill.skill_type === 'skill'
      ))
      .filter((skill) => {
        if (!query) return true;
        return [skill.name, skill.display_name, skill.description]
          .filter(Boolean)
          .join(' ')
          .toLowerCase()
          .includes(query);
      })
      .map((skill) => ({
        id: skill.name,
        label: skill.display_name || skill.name,
        description: skill.description,
        itemKind: 'skill' as const,
      }));
    return [...commands, ...skills];
  }
  const query = suggestion.query.trim().toLowerCase();
  return members
    .filter((item) => {
      if (!query) return true;
      return `${item.label} ${item.id}`.toLowerCase().includes(query);
    })
    .slice(0, 8);
}

function getProjectLabel(project: ProjectInfo | null, fallback: string): string {
  return project ? project.name : fallback;
}

function WorkIcon({ name, className }: { name: WorkIconName; className?: string }) {
  return <span className={cx('chat-work-icon', `chat-work-icon--${name}`, className)} aria-hidden="true" />;
}

function isDefaultProject(project: ProjectInfo): boolean {
  return project.is_default || project.project_id === 'default' || project.project_id === 'default_code';
}

interface InputAreaProps {
  onSubmit: (content: string, mediaItems?: MediaItem[]) => void;
  /** Signals that the user is editing an existing real Session. */
  onInputIntent?: (sessionId: string) => void;
  onPersistMedia: (content: string, mediaItems: MediaItem[]) => Promise<PersistMediaResponse>;
  onPersistDocuments: (content: string, mediaItems: MediaItem[]) => Promise<PersistMediaResponse>;
  onInterrupt: (newInput?: string) => void;
  onCancel: () => void;
  onSwitchMode: (mode: AgentMode) => void;
  isProcessing: boolean;
  autoFocusKey?: string | null;
  /** 跳转到技能管理页 */
  onNavigateToSkills?: () => void;
  /** 跳转到智能体管理页 */
  onNavigateToAgents?: () => void;
  permissionsEnabled: boolean;
  onSavePermission: (updates: Record<string, string>) => Promise<void>;
  /** 目标待设置态（"+"菜单选了「目标」）下发送时调用，取代普通 onSubmit/排队逻辑 */
  onSetGoal?: (sessionId: string, objective: string) => void;
  /** 工具栏"目标"标签的 × 按钮：目标已存在时点击等同删除目标 */
  onClearGoal?: (sessionId: string) => void;
  /**
   * 目标 active 时消息按设计走排队（见下方 isGoalActive 注释），但如果入队那一刻当前没有
   * 任何任务在处理，现有的自动排空触发点（chat.processing_status/interrupt_result）都要求
   * "之前在 processing"，不会命中，消息会永久卡住。入队后调用它兜底：内部会判断当前是否
   * 真的空闲，空闲才会真正发送，不会重复触发。
   */
  onDrainTaskQueueIfIdle?: (sessionId: string) => void;
}

export type InputAreaHandle = {
  appendLocalFilePicks: (picks: LocalFilePick[]) => void;
};

function clipboardHasFileItems(clipboardData: DataTransfer | null | undefined): boolean {
  if (!clipboardData) return false;
  if (Array.from(clipboardData.items || []).some((item) => item.kind === 'file')) return true;
  return Array.from(clipboardData.types || []).includes('Files');
}

const ACCEPTED_IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
/**
 * Keep in sync with jiuwenswarm/gateway/document_attachments.py
 * FORBIDDEN_DOCUMENT_EXTENSIONS.
 */
const FORBIDDEN_DOCUMENT_EXTENSIONS = new Set([
  '.exe',
  '.dll',
  '.msi',
  '.scr',
  '.bat',
  '.cmd',
  '.ps1',
  '.vbs',
  '.wsf',
  '.hta',
  '.jar',
  '.lnk',
  '.bin',
  '.so',
  '.dylib',
  '.app',
  '.dmg',
  '.pkg',
  '.command',
  '.scpt',
  '.scptd',
  '.workflow',
  '.xpc',
  '.bundle',
  '.framework',
  '.kext',
  '.prefpane',
  '.saver',
  '.component',
]);
/**
 * Dialog filter only (not a security boundary). Intentionally omits blacklist
 * extensions. Do NOT append star-slash-star (all MIME); Windows then collapses
 * to image-only. Final allow/deny still uses FORBIDDEN_DOCUMENT_EXTENSIONS in JS.
 */
const ATTACHMENT_ACCEPT = [
  'image/*',
  '.png',
  '.jpg',
  '.jpeg',
  '.webp',
  '.gif',
  '.bmp',
  '.svg',
  '.ico',
  '.pdf',
  '.doc',
  '.docx',
  '.xls',
  '.xlsx',
  '.ppt',
  '.pptx',
  '.txt',
  '.md',
  '.markdown',
  '.csv',
  '.tsv',
  '.rtf',
  '.odt',
  '.ods',
  '.odp',
  '.json',
  '.xml',
  '.yaml',
  '.yml',
  '.html',
  '.htm',
  '.css',
  '.js',
  '.ts',
  '.tsx',
  '.jsx',
  '.py',
  '.java',
  '.c',
  '.cpp',
  '.h',
  '.go',
  '.rs',
  '.rb',
  '.php',
  '.sql',
  '.ipynb',
  '.toml',
  '.ini',
  '.log',
  '.zip',
  '.rar',
  '.7z',
  '.tar',
  '.gz',
  'audio/*',
  'video/*',
]
  .filter((item) => !FORBIDDEN_DOCUMENT_EXTENSIONS.has(item.toLowerCase()))
  .join(',');
const IMAGE_EXTENSIONS = new Set(['.png', '.jpg', '.jpeg', '.webp', '.gif']);
const MAX_FILE_BYTES = 100 * 1024 * 1024;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_ATTACHMENT_COUNT = 20;
const ATTACHMENT_ALERT_DURATION_MS = 3000;

type AttachmentKind = 'image' | 'document';
type AttachmentStatus = 'uploading' | 'ready' | 'error';

interface AttachmentDraft {
  id: string;
  kind: AttachmentKind;
  filename: string;
  mimeType: string;
  size: number;
  status: AttachmentStatus;
  base64Data?: string;
  previewUrl?: string;
  persistedMediaItem?: Record<string, unknown>;
  error?: string;
  file?: File;
  /** Absolute local path from desktop native picker (WebView2 has no File.path). */
  localPath?: string;
}

interface AttachmentAlert {
  id: string;
  message: string;
}

interface PersistMediaResponse {
  content?: string;
  query?: string;
  media_items?: Record<string, unknown>[];
  files?: Record<string, unknown>;
}

function formatAttachmentSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function makeAttachmentId(file: File): string {
  return `${file.name || 'attachment'}-${file.size}-${generateUuidV4()}`;
}

function getFileExtension(filename: string): string {
  const idx = filename.lastIndexOf('.');
  if (idx < 0) return '';
  return filename.slice(idx).toLowerCase();
}

function attachmentToMediaItem(attachment: AttachmentDraft): MediaItem {
  const persisted = attachment.persistedMediaItem;
  const filename = pickString(persisted?.filename) || attachment.filename;
  const mimeType = pickString(persisted?.mime_type, persisted?.mimeType) || attachment.mimeType;
  const sizeBytes = pickNumber(persisted?.size_bytes, persisted?.sizeBytes) ?? attachment.size;
  const path = pickString(persisted?.path);
  // After persist, only send path metadata — never re-send base64 on chat.send.
  return {
    type: attachment.kind,
    mimeType,
    mime_type: mimeType,
    filename,
    ...(path ? { path } : { base64Data: attachment.base64Data }),
    sizeBytes,
    size_bytes: sizeBytes,
  };
}

function buildUploadMediaItem(attachment: AttachmentDraft, payload: Pick<AttachmentDraft, 'base64Data'>): MediaItem {
  return {
    type: attachment.kind,
    mimeType: attachment.mimeType,
    filename: attachment.filename,
    base64Data: payload.base64Data,
  };
}

function pickString(...values: unknown[]): string | undefined {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value;
    }
  }
  return undefined;
}

function pickNumber(...values: unknown[]): number | undefined {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
  }
  return undefined;
}

function isImageFile(file: File): boolean {
  if (ACCEPTED_IMAGE_TYPES.has(file.type)) return true;
  return IMAGE_EXTENSIONS.has(getFileExtension(file.name || ''));
}

function isForbiddenDocumentFile(file: File): boolean {
  const ext = getFileExtension(file.name || '');
  return Boolean(ext) && FORBIDDEN_DOCUMENT_EXTENSIONS.has(ext);
}

function isDocumentFile(file: File): boolean {
  if (isImageFile(file)) return false;
  return !isForbiddenDocumentFile(file);
}

/** Local absolute path when available (desktop native picker / Electron File.path). */
function getLocalFilePath(file: File | undefined, explicitPath?: string): string | undefined {
  if (typeof explicitPath === 'string' && explicitPath.trim()) {
    return explicitPath.trim();
  }
  if (!file) return undefined;
  const maybePath = (file as File & { path?: string }).path;
  if (typeof maybePath === 'string' && maybePath.trim()) {
    return maybePath.trim();
  }
  return undefined;
}

/** Classify a picked file for routing to media.persist vs document.persist. */
function resolveAttachmentKind(file: File): AttachmentKind | null {
  if (isImageFile(file)) return 'image';
  if (isForbiddenDocumentFile(file)) return null;
  return 'document';
}

function getImageValidationError(file: File, t: TFunction): string | null {
  if (!isImageFile(file)) {
    return t('chat.inputAttachment.unsupportedFileType', { name: file.name || t('chat.inputAttachment.unnamedFile') });
  }
  if (file.size > MAX_FILE_BYTES) {
    return t('chat.inputAttachment.fileSizeExceeded', { name: file.name || t('chat.inputAttachment.unnamedFile'), limit: formatAttachmentSize(MAX_FILE_BYTES) });
  }
  return null;
}

function clearAttachmentAlertTimers(timers: Map<string, number>): void {
  timers.forEach((timeoutId) => window.clearTimeout(timeoutId));
  timers.clear();
}

function getDocumentValidationError(
  file: File | undefined,
  t: TFunction,
  options?: { filename?: string; localPath?: string },
): string | null {
  const filename = options?.filename || file?.name || t('chat.inputAttachment.unnamedFile');
  if (file && file.size > MAX_FILE_BYTES) {
    return t('chat.inputAttachment.fileSizeExceeded', { name: filename, limit: formatAttachmentSize(MAX_FILE_BYTES) });
  }
  if (file && isForbiddenDocumentFile(file)) {
    return t('chat.inputAttachment.forbiddenFileType', { name: filename });
  }
  if (file && !isDocumentFile(file)) {
    return t('chat.inputAttachment.unsupportedFileType', { name: filename });
  }
  if (!getLocalFilePath(file, options?.localPath)) {
    return t('chat.inputAttachment.localPathUnavailable', { name: filename });
  }
  return null;
}

function readBinaryFileAsBase64(file: File): Promise<Pick<AttachmentDraft, 'base64Data' | 'previewUrl'> | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = typeof reader.result === 'string' ? reader.result : '';
      const base64Data = result.includes(',') ? result.split(',')[1] : '';
      if (!base64Data) {
        resolve(null);
        return;
      }
      resolve({
        base64Data,
        previewUrl: ACCEPTED_IMAGE_TYPES.has(file.type) ? result : undefined,
      });
    };
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

function readImageFile(file: File, t: TFunction): Promise<Pick<AttachmentDraft, 'base64Data' | 'previewUrl'> | null> {
  if (getImageValidationError(file, t)) {
    return Promise.resolve(null);
  }
  return readBinaryFileAsBase64(file);
}

function buildSubmitContent(text: string, attachments: AttachmentDraft[]): string {
  const docs = attachments.filter((item) => item.kind === 'document' && item.status === 'ready');
  if (!docs.length) {
    return text;
  }
  // Agent-facing @path refs only (stripped from chat bubble). No parse / no sidecar.
  // Paths may be missing on a brand-new session before persist; useWebSocket
  // rewrites this block after document.persist returns real paths.
  return withUploadDocumentBlock(
    text,
    docs.map((doc) => ({
      filename: doc.filename,
      path: pickString(doc.persistedMediaItem?.path),
      originalPath: pickString(doc.persistedMediaItem?.original_path, doc.persistedMediaItem?.path),
    })),
  );
}

export const InputArea = forwardRef<InputAreaHandle, InputAreaProps>(function InputArea(
  {
    onSubmit,
    onInputIntent,
    onPersistMedia,
    onPersistDocuments,
    onInterrupt,
    onCancel,
    onSwitchMode,
    isProcessing,
    autoFocusKey = null,
    onNavigateToSkills,
    onNavigateToAgents,
    permissionsEnabled,
    onSavePermission,
    onSetGoal,
    onClearGoal,
    onDrainTaskQueueIfIdle,
  },
  ref,
) {
  const [pendingVoiceText, setPendingVoiceText] = useState('');
  const [isModeMenuOpen, setIsModeMenuOpen] = useState(false);
  const [attachments, setAttachments] = useState<AttachmentDraft[]>([]);
  const [attachmentAlerts, setAttachmentAlerts] = useState<AttachmentAlert[]>([]);
  const attachmentAlertTimersRef = useRef<Map<string, number>>(new Map());
  const [attachmentMenuId, setAttachmentMenuId] = useState<string | null>(null);
  const [attachmentMenuAnchor, setAttachmentMenuAnchor] = useState<DOMRect | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [workMenuOpen, setWorkMenuOpen] = useState<'project' | null>(null);
  const [workDialogOpen, setWorkDialogOpen] = useState(false);
  const [projectNameDraft, setProjectNameDraft] = useState('');
  const [projectDirDraft, setProjectDirDraft] = useState('');
  const [projectDirError, setProjectDirError] = useState<string | null>(null);
  const [projectSearch, setProjectSearch] = useState('');
  const [projectCreateMode, setProjectCreateMode] = useState<ProjectCreateMode>('blank');
  const [menuDirection, setMenuDirection] = useState<'up' | 'down'>('up');
  const [agentPickerOpen, setAgentPickerOpen] = useState(false);
  const [agentPickerQuery, setAgentPickerQuery] = useState('');
  const { tooltip: agentTooltipNode, handlers: agentTooltipHandlers } = useAdaptiveTooltip({ offsetX: -50 });
  const { tooltip: attachTooltipNode, handlers: attachTooltipHandlers } = useAdaptiveTooltip();
  const [hoveredOptionDesc, setHoveredOptionDesc] = useState<string | null>(null);
  const [hoveredOptionRect, setHoveredOptionRect] = useState<DOMRect | null>(null);
  const [agentOptions, setAgentOptions] = useState<AgentCatalogItem[]>([]);
  const [agentOptionsStatus, setAgentOptionsStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle');
  const agentManagementClient = useMemo(() => createAgentManagementClient(), []);

  useEffect(() => {
    if (!projectDirError || workDialogOpen) return;
    const timeoutId = window.setTimeout(() => setProjectDirError(null), 3000);
    return () => window.clearTimeout(timeoutId);
  }, [projectDirError, workDialogOpen]);

  const [composerSuggestion, setComposerSuggestion] = useState<ComposerSuggestionState | null>(null);
  const [composerSuggestionIndex, setComposerSuggestionIndex] = useState(0);
  const [composerSuggestionNavigationMode, setComposerSuggestionNavigationMode] = useState<'keyboard' | 'pointer'>('pointer');
  const [compactingSessionIds, setCompactingSessionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [slashCommands, setSlashCommands] = useState<SlashCommandMeta[]>([]);
  const [slashSkills, setSlashSkills] = useState<InputAreaSkillItem[]>([]);
  const [slashCatalogLoading, setSlashCatalogLoading] = useState(false);
  const [slashCatalogLoaded, setSlashCatalogLoaded] = useState(false);
  const [modeMenuAnchor, setModeMenuAnchor] = useState<DOMRect | null>(null);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [attachMenuAnchor, setAttachMenuAnchor] = useState<DOMRect | null>(null);
  // 默认下弹（这是本轮"扩展"需求里明确要的方向），但会话有消息、输入框沉到视口底部时，"+"按钮
  // 本身已经贴近视口下边缘，下弹会把整个菜单渲染到可视区域外面、用户点了完全没反应——2026-08-18
  // 用户报告的严重 bug。这里补上跟同文件里 modeMenu/model-selector 菜单一致的"空间不够就翻上去"
  // 兜底逻辑，只在下方空间不够时才翻上，正常情况仍然默认下弹。
  const [attachMenuDirection, setAttachMenuDirection] = useState<'up' | 'down'>('down');
  const [extensionPanelOpen, setExtensionPanelOpen] = useState(false);
  const [skillPanelOpen, setSkillPanelOpen] = useState(false);
  const [swarmflowConfigPanelOpen, setSwarmflowConfigPanelOpen] = useState(false);
  const [swarmflowConfigAnchor, setSwarmflowConfigAnchor] = useState<DOMRect | null>(null);
  const inputRef = useRef<HTMLDivElement>(null);
  const insertSkillChipRef = useRef<(skillName: string) => void>(() => undefined);
  /** 保存技能插入前的光标位置，用于在光标处插入 chip */
  const savedRangeRef = useRef<Range | null>(null);
  const modeMenuRef = useRef<HTMLDivElement>(null);
  const workMenuRef = useRef<HTMLDivElement>(null);
  const modeMenuPortalRef = useRef<HTMLDivElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);
  const attachMenuPortalRef = useRef<HTMLDivElement>(null);
  const extensionPanelRef = useRef<HTMLDivElement>(null);
  const composerFrameRef = useRef<HTMLDivElement>(null);
  const composerSuggestionMenuRef = useRef<HTMLDivElement>(null);
  const compactingSessionIdsRef = useRef<Set<string>>(new Set());
  const skillPanelRef = useRef<HTMLDivElement>(null);
  const swarmflowConfigBtnRef = useRef<HTMLButtonElement>(null);
  const swarmflowConfigPanelRef = useRef<HTMLDivElement>(null);
  const autoSendTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attachmentMenuTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attachmentMenuOpenedByLongPressRef = useRef(false);
  const isComposingRef = useRef(false);
  // const activePointerIdRef = useRef<number | null>(null);
  const isVoicePressingRef = useRef(false);
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const hasPendingQuestion = useChatStore(
    (s) => Boolean(s.runtimes[activeSessionId ?? '']?.pendingQuestions[0]),
  );
  const isCompactRunning = Boolean(
    activeSessionId && compactingSessionIds.has(activeSessionId),
  );
  const composerDisabled = isCompactRunning || hasPendingQuestion;
  const selectedAgentId = useSessionStore((s) => {
    const runtime = s.runtimes[activeSessionId ?? ''];
    if (runtime?.mode !== 'agent') return null;
    const intent = runtime.agentSelectionIntent;
    return intent?.kind === 'select' ? intent.id : null;
  });
  const setAgentSelectionIntent = useSessionStore((s) => s.setAgentSelectionIntent);
  const selectedAgent = agentOptions.find((item) => item.id === selectedAgentId) ?? null;
  const installedAgentOptions = useMemo(
    () => agentOptions.filter((item) => item.installed && item.connectionState === 'connected' && item.enabled !== false),
    [agentOptions],
  );
  const filteredAgentOptions = useMemo(() => {
    const query = agentPickerQuery.trim().toLocaleLowerCase();
    if (!query) return installedAgentOptions;
    return installedAgentOptions.filter((item) =>
      `${item.displayName} ${item.description} ${item.category}`.toLocaleLowerCase().includes(query),
    );
  }, [agentPickerQuery, installedAgentOptions]);

  useEffect(() => {
    if (!activeSessionId || (!agentPickerOpen && !selectedAgentId)) return;
    let cancelled = false;
    setAgentOptionsStatus('loading');
    void agentManagementClient.listCatalog()
      .then((items) => {
        if (cancelled) return;
        const selectedItem = selectedAgentId ? items.find((item) => item.id === selectedAgentId) : null;
        if (selectedItem?.enabled === false || selectedItem?.connectionState !== 'connected') {
          setAgentSelectionIntent(activeSessionId, { kind: 'clear' });
        }
        setAgentOptions(items);
        setAgentOptionsStatus('success');
      })
      .catch(() => {
        if (!cancelled) setAgentOptionsStatus('error');
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId, agentManagementClient, agentPickerOpen, selectedAgentId]);

  useEffect(() => {
    if (attachMenuOpen) return;
    setAgentPickerOpen(false);
    setAgentPickerQuery('');
  }, [attachMenuOpen]);
  const isPaused = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.isPaused ?? false);
  const queuePaused = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.queuePaused ?? false);
  const isLoadingHistory = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.isLoadingHistory ?? false);
  const inputValue = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.inputValue ?? '');
  const evolutionStatus = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.evolutionStatus ?? null);
  const mode = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.mode ?? 'agent');
  const selectedSkills = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.selectedSkills ?? []);
  const teamMembers = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamMembers ?? []) as InputAreaTeamMember[];
  const currentSession = useSessionStore((s) => s.currentSession);
  const activeSession = useSessionStore((s) => {
    if (!activeSessionId || activeSessionId === NEW_CONVERSATION_ID) return null;
    if (s.currentSession?.session_id === activeSessionId) return s.currentSession;
    return s.sessions.find((session) => session.session_id === activeSessionId) ?? null;
  });
  const canPersistAttachments = Boolean(activeSessionId && activeSessionId !== NEW_CONVERSATION_ID);
  const {
    workMode,
    projects,
    selectedProject,
    setSelectedProject,
    createProject,
  } = useWorkspaceStore();
  const loadedMsgLen = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.messages?.length ?? 0);
  const hasHistory = (currentSession?.message_count ?? 0) > 0 || loadedMsgLen > 0;
  const goalArmed = useGoalStore((s) => s.runtimes[activeSessionId ?? '']?.armed ?? false);
  const currentGoal = useGoalStore((s) => s.runtimes[activeSessionId ?? '']?.goal ?? null);
  // 目标 active 时普通发送改走排队，而不是文档 §5.1 原定的 input_mode:'steer' 实时插话——
  // 用户明确要求改成这个语义（steer 目前收不到任何反馈，体验上等同于消息发出去石沉大海，
  // 见 backend-requests.md #1）。走排队后消息复用现有的通用队列机制，行为和普通排队一致。
  const isGoalActive = currentGoal?.status === 'active';
  // 未完成目标：active/paused/blocked 都算，只有 completed（或没有目标）才能再设新目标
  const hasUnfinishedGoal = isUnfinishedGoal(currentGoal);
  const isInterruptible = isProcessing || isPaused || isGoalActive;
  const isAgentMode = mode === 'agent';
  const isTeamMode = mode === 'team';
  const isAutoHarnessMode = mode === 'auto_harness';

  useEffect(() => {
    if (!isTeamMode) return;
    setAgentPickerOpen(false);
    setAgentPickerQuery('');
  }, [isTeamMode]);

  const isWorkContextLocked = Boolean(activeSessionId && activeSessionId !== NEW_CONVERSATION_ID);
  const showWorkContextRow = activeSessionId === NEW_CONVERSATION_ID;
  /** Goal 入口是否适用于当前上下文（agent 模式 + 已接入 onSetGoal，如欢迎页新会话就不适用） */
  const canUseGoalMenu = isAgentMode && Boolean(onSetGoal);
  // 只跟 armed 挂钩：这个 tag 是"下一条消息将用于设置目标"的过渡态指示，发送后 armed 变 false
  // 就该跟着消失，不能靠"目标是否存在"续命——目标存在与否、当前状态、编辑/暂停/删除，已经由
  // 输入框上方常驻的 GoalBar 完整覆盖，工具栏这里再挂一份重复的常驻入口只会显得"选择没解除"。
  const goalTagVisible = canUseGoalMenu && goalArmed;
  // Plan 是持续开关（不是 Goal 那种"下一条消息生效"的过渡态）：打开后一直用
  // agent.plan 发送，直到用户点叉或后端推 plan.mode_exited。
  // 和 Goal 一样只对单 agent 开放，集群模式不提供 Plan 入口。
  const planActive = usePlanStore((s) => s.runtimes[activeSessionId ?? '']?.active ?? false);
  const planPendingExplicitEntry = usePlanStore(
    (s) => s.runtimes[activeSessionId ?? '']?.pendingExplicitEntry ?? false,
  );
  // Reactive selectors for swarmflow state. Using getState() inside render IIFEs
  // does not subscribe the component to store changes, leaving the Switch/UI stale
  // when swarmflow is toggled off.
  const swarmflowActive = useSessionStore(
    (s) => s.runtimes[activeSessionId ?? '']?.enableSwarmflow ?? false,
  );
  const swarmflowBudget = useSessionStore(
    (s) => s.runtimes[activeSessionId ?? '']?.swarmflowBudget ?? null,
  );
  // 进入真实会话后开关只读：仅新建对话页可修改，真实会话可查看不可改
  const swarmflowToggleDisabled = isProcessing || (activeSessionId !== NEW_CONVERSATION_ID && hasHistory);
  // Plan 已经真正生效：开关打开且至少发出过一条 Plan 消息（pendingExplicitEntry 已被消费）。
  // 区别于"刚打开开关但还没发消息"的未提交态——后者和 Goal 的 armed 一样可以被对方随手顶替。
  const planCommitted = planActive && !planPendingExplicitEntry;
  const canUsePlanMenu = supportsPlanMode(mode);
  const planTagVisible = canUsePlanMenu && planActive;

  const mentionableMembers = useMemo(() => {
    return teamMembers
      .filter((member) => {
        const id = member.member_id?.trim();
        return id && id !== 'user';
      })
      .map((member) => ({
        id: member.member_id,
        label: member.name || member.member_id,
        status: member.status || '',
      }));
  }, [teamMembers]);

  const composerSuggestionItems = useMemo(() => {
    const items = getComposerSuggestionItems(
      composerSuggestion,
      mentionableMembers,
      getWebSlashCommandsForMode(slashCommands, mode),
      slashSkills,
      isTeamMode,
    );
    return items.map((item) => (
      item.itemKind === 'command' && isSlashCommandDisabledByGoal(item.id, hasUnfinishedGoal)
        ? { ...item, disabled: true, disabledReason: t('plan.toolbarUnavailableGoal') }
        : item
    ));
  }, [composerSuggestion, hasUnfinishedGoal, isTeamMode, mentionableMembers, mode, slashCommands, slashSkills, t]);

  const selectableComposerSuggestionIndices = useMemo(
    () => composerSuggestionItems.reduce<number[]>((indices, item, index) => {
      if (!item.disabled) indices.push(index);
      return indices;
    }, []),
    [composerSuggestionItems],
  );

  useEffect(() => {
    if (composerSuggestion?.kind !== 'slash' || slashCatalogLoaded || slashCatalogLoading) return;
    setSlashCatalogLoading(true);
    void Promise.all([
      webRequest<{ commands?: SlashCommandMeta[] }>(
        'commands.list',
        { work_mode: activeSession?.work_mode ?? workMode },
      ),
      webRequest<{ skills?: InputAreaSkillItem[]; plugins?: InputAreaInstalledPlugin[] }>(
        'skills.list',
        { with_installed: true },
        { timeoutMs: 30_000 },
      ),
    ]).then(([commandData, skillData]) => {
      const installedNames = buildInstalledSkillNames(skillData.plugins ?? []);
      const availableSkills = filterEnabledMySkills(skillData.skills ?? [], installedNames);
      setSlashCommands(commandData.commands ?? []);
      setSlashSkills(availableSkills);
      setSlashCatalogLoaded(true);
    }).catch((error) => {
      console.error('Failed to load slash command catalog:', error);
    }).finally(() => {
      setSlashCatalogLoading(false);
    });
  }, [activeSession?.work_mode, composerSuggestion?.kind, slashCatalogLoaded, workMode]);

  useEffect(() => {
    setComposerSuggestionIndex(selectableComposerSuggestionIndices[0] ?? -1);
  }, [composerSuggestion?.kind, composerSuggestion?.query, selectableComposerSuggestionIndices]);

  useEffect(() => {
    setComposerSuggestionNavigationMode('pointer');
  }, [composerSuggestion?.kind]);

  const moveComposerSuggestionHighlight = useCallback((delta: 1 | -1) => {
    if (selectableComposerSuggestionIndices.length === 0) return;
    setComposerSuggestionIndex((current) => {
      const position = selectableComposerSuggestionIndices.indexOf(current);
      if (position === -1) {
        return delta > 0
          ? selectableComposerSuggestionIndices[0]
          : selectableComposerSuggestionIndices[selectableComposerSuggestionIndices.length - 1];
      }
      const nextPosition = (
        position + delta + selectableComposerSuggestionIndices.length
      ) % selectableComposerSuggestionIndices.length;
      return selectableComposerSuggestionIndices[nextPosition];
    });
  }, [selectableComposerSuggestionIndices]);

  const inputProjectOptions = useMemo(
    () => getInputProjectOptions(projects, projectSearch),
    [projectSearch, projects],
  );
  const hasInputProjectOptions = useMemo(
    () => getInputProjectOptions(projects).length > 0,
    [projects],
  );

  const displayedProject = useMemo<ProjectInfo | null>(() => {
    if (activeSession?.project_id
      && activeSession.project_id !== 'default'
      && activeSession.project_id !== 'default_code') {
      const matched = projects.find((project) => project.project_id === activeSession.project_id);
      if (matched && !isDefaultProject(matched)) return matched;
    }
    if (activeSession?.project_dir) {
      const matched = projects.find((project) => project.project_dir === activeSession.project_dir);
      if (matched && !isDefaultProject(matched)) return matched;
      const path = activeSession.project_dir || '';
      return {
        project_id: activeSession.project_id || path,
        project_dir: path,
        name: path.split('/').filter(Boolean).pop() || t('multiSession.project.projects'),
        pinned: false,
        pin_order: 0,
        is_default: path === '',
        hidden: false,
        work_mode: activeSession.work_mode ?? workMode,
        git: {
          enabled: false,
          repo_root: '',
          initialized_by_jiuwenswarm: false,
          detected_at: 0,
          status: 'disabled',
          branch: '',
          error: '',
          is_dirty: false,
        },
        session_count: 0,
        last_message_at: null,
        last_user_message_at: null,
        created_at: 0,
      };
    }
    return selectedProject && !isDefaultInputProject(selectedProject) ? selectedProject : null;
  }, [activeSession, projects, selectedProject, t, workMode]);

  const {
    isListening,
    // startListening,
    stopListening,
    // isSupported: speechSupported,
  } = useSpeechRecognition({
    language: 'cmn-Hans-CN',
    continuous: true,
    interimResults: true,
    silenceTimeoutMs: 8000,
    restartWhen: () => isVoicePressingRef.current,
    onResult: (text, isFinal) => {
      if (isFinal) {
        setPendingVoiceText((prev) => prev + text);
      }
    },
    onEnd: () => {
      autoSendTimeoutRef.current = setTimeout(() => {}, 100);
    },
    onError: (error) => {
      console.error('Speech recognition error:', error);
    },
  });

  const imageInputDisabled = isImageInputDisabled({
    isListening,
    isCompactRunning,
    isInterruptible,
    isTeamMode,
    isAgentMode,
  });
  const isDesktopBridgeReady = useDesktopLocalFilePickerReady();
  // "+" 触发按钮本身不跟图片/目标的可用性挂钩：菜单以后可能挂其他跟图片/目标无关的功能，
  // 触发按钮只要不在录音就该能点开；具体某一项能不能选，交给菜单里每一项各自的禁用态处理。
  const attachTriggerDisabled = isListening || composerDisabled;
  const readyAttachments = useMemo(
    () =>
      attachments.filter(
        (attachment) =>
          attachment.status === 'ready' &&
          (Boolean(pickString(attachment.persistedMediaItem?.path)) || Boolean(attachment.base64Data)),
      ),
    [attachments],
  );
  const hasUploadingAttachments = attachments.some((attachment) => attachment.status === 'uploading');
  const hasAttachmentErrors = attachments.some((attachment) => attachment.status === 'error');
  const readyMediaItems = useMemo(
    () => readyAttachments.map(attachmentToMediaItem),
    [readyAttachments],
  );

  useEffect(() => {
    if (!isListening && pendingVoiceText) {
      const finalText = (inputValue + pendingVoiceText).trim();
      if (finalText) {
        const sid = useChatStore.getState().activeSessionId;
        if (sid) {
          useChatStore.getState().setInputValue(sid, finalText);
        }
        setPendingVoiceText('');
        if (composerDisabled) return;

        setTimeout(() => {
          if (sid && useChatStore.getState().runtimes[sid]?.pendingQuestions[0]) return;
          if (isTeamMode) {
            onSubmit(finalText);
          } else if (isInterruptible) {
            onInterrupt(finalText);
          } else {
            onSubmit(finalText);
          }
          if (sid) {
            useChatStore.getState().setInputValue(sid, '');
          }
        }, 150);
      }
    }
  }, [composerDisabled, isListening, pendingVoiceText, inputValue, isInterruptible, isTeamMode, onSubmit, onInterrupt]);

  useEffect(() => {
    return () => {
      if (autoSendTimeoutRef.current) {
        clearTimeout(autoSendTimeoutRef.current);
      }
      if (attachmentMenuTimerRef.current) {
        clearTimeout(attachmentMenuTimerRef.current);
      }
      clearAttachmentAlertTimers(attachmentAlertTimersRef.current);
    };
  }, []);

  const pushAttachmentAlert = useCallback((message: string) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const timers = attachmentAlertTimersRef.current;
    while (timers.size >= 3) {
      const oldestId = timers.keys().next().value;
      if (oldestId === undefined) break;
      const oldestTimeoutId = timers.get(oldestId);
      if (oldestTimeoutId !== undefined) {
        window.clearTimeout(oldestTimeoutId);
      }
      timers.delete(oldestId);
    }
    const timeoutId = window.setTimeout(() => {
      timers.delete(id);
      setAttachmentAlerts((prev) => prev.filter((item) => item.id !== id));
    }, ATTACHMENT_ALERT_DURATION_MS);
    timers.set(id, timeoutId);
    setAttachmentAlerts((prev) => [
      ...prev.filter((item) => timers.has(item.id)),
      { id, message },
    ].slice(-3));
  }, []);

  const dismissAttachmentAlert = useCallback((id: string) => {
    const timeoutId = attachmentAlertTimersRef.current.get(id);
    if (timeoutId !== undefined) {
      window.clearTimeout(timeoutId);
      attachmentAlertTimersRef.current.delete(id);
    }
    setAttachmentAlerts((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const updateAttachment = useCallback((id: string, update: Partial<AttachmentDraft>) => {
    setAttachments((prev) => prev.map((item) => (
      item.id === id ? { ...item, ...update } : item
    )));
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((item) => item.id !== id));
    setAttachmentMenuId((current) => (current === id ? null : current));
  }, []);

  const clearAttachments = useCallback(() => {
    setAttachments([]);
    setAttachmentAlerts([]);
    setAttachmentMenuId(null);
    clearAttachmentAlertTimers(attachmentAlertTimersRef.current);
  }, []);

  const stopAttachmentMenuTimer = useCallback(() => {
    if (attachmentMenuTimerRef.current) {
      clearTimeout(attachmentMenuTimerRef.current);
      attachmentMenuTimerRef.current = null;
    }
  }, []);

  const startAttachmentMenuTimer = useCallback((id: string, el: HTMLElement) => {
    stopAttachmentMenuTimer();
    attachmentMenuOpenedByLongPressRef.current = false;
    attachmentMenuTimerRef.current = setTimeout(() => {
      attachmentMenuOpenedByLongPressRef.current = true;
      setAttachmentMenuAnchor(el.getBoundingClientRect());
      setAttachmentMenuId(id);
    }, 520);
  }, [stopAttachmentMenuTimer]);

  const handleAttachmentRemoveClick = useCallback((id: string) => {
    if (attachmentMenuOpenedByLongPressRef.current || attachmentMenuId === id) {
      attachmentMenuOpenedByLongPressRef.current = false;
      return;
    }
    removeAttachment(id);
  }, [attachmentMenuId, removeAttachment]);

  const uploadAttachment = useCallback((attachment: AttachmentDraft) => {
    const validationError =
      attachment.kind === 'document'
        ? getDocumentValidationError(attachment.file, t, {
            filename: attachment.filename,
            localPath: attachment.localPath,
          })
        : attachment.file
          ? getImageValidationError(attachment.file, t)
          : (attachment.base64Data ? null : t('chat.inputAttachment.unsupportedFileType', { name: attachment.filename || t('chat.inputAttachment.unnamedFile') }));
    if (validationError) {
      pushAttachmentAlert(validationError);
      updateAttachment(attachment.id, { status: 'error', error: validationError });
      return;
    }
    updateAttachment(attachment.id, { status: 'uploading', error: undefined });

    // Documents: validate local path only — no base64 transfer / no disk persist / no parse.
    if (attachment.kind === 'document') {
      const localPath = getLocalFilePath(attachment.file, attachment.localPath);
      if (!localPath) {
        const error = t('chat.inputAttachment.localPathUnavailable', { name: attachment.filename || t('chat.inputAttachment.unnamedFile') });
        pushAttachmentAlert(error);
        updateAttachment(attachment.id, { status: 'error', error });
        return;
      }
      void (async () => {
        if (!canPersistAttachments) {
          updateAttachment(attachment.id, {
            persistedMediaItem: {
              type: 'document',
              filename: attachment.filename,
              mime_type: attachment.mimeType,
              path: localPath,
              original_path: localPath,
              size_bytes: attachment.size,
            },
            status: 'ready',
            error: undefined,
          });
          return;
        }
        try {
          const persisted = await onPersistDocuments('', [
            {
              type: 'document',
              mimeType: attachment.mimeType,
              filename: attachment.filename,
              path: localPath,
              sizeBytes: attachment.size,
              size_bytes: attachment.size,
            },
          ]);
          const persistedMediaItem = persisted.media_items?.[0];
          if (!persistedMediaItem || !pickString(persistedMediaItem.path)) {
            throw new Error('document.persist did not return document path');
          }
          updateAttachment(attachment.id, {
            base64Data: undefined,
            persistedMediaItem,
            status: 'ready',
            error: undefined,
          });
        } catch (error) {
          console.error('Document upload failed:', error);
          updateAttachment(attachment.id, {
            status: 'error',
            error: t('chat.inputAttachment.uploadFailed'),
          });
        }
      })();
      return;
    }

    // Desktop native picker may already include base64 for images.
    if (attachment.base64Data) {
      void (async () => {
        const payload = {
          base64Data: attachment.base64Data,
          previewUrl: attachment.previewUrl,
        };
        if (!canPersistAttachments) {
          updateAttachment(attachment.id, {
            ...payload,
            status: 'ready',
            error: undefined,
          });
          return;
        }
        try {
          const persisted = await onPersistMedia('', [buildUploadMediaItem(attachment, payload)]);
          const persistedMediaItem = persisted.media_items?.[0];
          if (!persistedMediaItem || !pickString(persistedMediaItem.path)) {
            throw new Error('media.persist did not return image path');
          }
          updateAttachment(attachment.id, {
            ...payload,
            base64Data: undefined,
            persistedMediaItem,
            status: 'ready',
            error: undefined,
          });
        } catch (error) {
          console.error('Image upload failed:', error);
          updateAttachment(attachment.id, {
            ...payload,
            status: 'error',
            error: t('chat.inputAttachment.uploadFailed'),
          });
        }
      })();
      return;
    }

    if (!attachment.file) {
      const error = t('chat.inputAttachment.uploadFailed');
      pushAttachmentAlert(error);
      updateAttachment(attachment.id, { status: 'error', error });
      return;
    }

    void readImageFile(attachment.file, t).then(async (payload) => {
      if (!payload) {
        updateAttachment(attachment.id, {
          status: 'error',
          error: t('chat.inputAttachment.uploadFailed'),
        });
        return;
      }
      if (!canPersistAttachments) {
        updateAttachment(attachment.id, {
          ...payload,
          status: 'ready',
          error: undefined,
        });
        return;
      }
      try {
        const persisted = await onPersistMedia('', [buildUploadMediaItem(attachment, payload)]);
        const persistedMediaItem = persisted.media_items?.[0];
        if (!persistedMediaItem || !pickString(persistedMediaItem.path)) {
          throw new Error('media.persist did not return image path');
        }
        updateAttachment(attachment.id, {
          ...payload,
          base64Data: undefined,
          persistedMediaItem,
          status: 'ready',
          error: undefined,
        });
      } catch (error) {
        console.error('Image upload failed:', error);
        updateAttachment(attachment.id, {
          ...payload,
          status: 'error',
          error: t('chat.inputAttachment.uploadFailed'),
        });
      }
    });
  }, [canPersistAttachments, onPersistDocuments, onPersistMedia, pushAttachmentAlert, updateAttachment, t]);

  const retryAttachment = useCallback((attachment: AttachmentDraft) => {
    uploadAttachment(attachment);
  }, [uploadAttachment]);

  const appendAttachmentFiles = useCallback((files: FileList | File[]) => {
    const selectedFiles = Array.from(files);
    if (!selectedFiles.length) return;

    const remaining = MAX_ATTACHMENT_COUNT - attachments.length;
    if (remaining <= 0) {
      pushAttachmentAlert(t('chat.inputAttachment.attachmentCountExceeded', { limit: MAX_ATTACHMENT_COUNT }));
      return;
    }

    const filesToAdd = selectedFiles.slice(0, remaining);
    if (selectedFiles.length > remaining) {
      pushAttachmentAlert(t('chat.inputAttachment.attachmentCountPartialAdd', { limit: MAX_ATTACHMENT_COUNT, count: remaining }));
    }

    const drafts = filesToAdd.reduce<AttachmentDraft[]>((items, file) => {
      const kind = resolveAttachmentKind(file);
      if (!kind) {
        const message = isForbiddenDocumentFile(file)
          ? t('chat.inputAttachment.forbiddenFileType', { name: file.name || t('chat.inputAttachment.unnamedFile') })
          : t('chat.inputAttachment.unsupportedFileType', { name: file.name || t('chat.inputAttachment.unnamedFile') });
        pushAttachmentAlert(message);
        return items;
      }
      const localPath = getLocalFilePath(file);
      const base = {
        id: makeAttachmentId(file),
        kind,
        filename: file.name || (kind === 'document' ? `document-${Date.now()}` : `image-${Date.now()}`),
        mimeType: file.type || 'application/octet-stream',
        size: file.size,
        file,
        ...(localPath ? { localPath } : {}),
      };
      const validationError =
        kind === 'document'
          ? getDocumentValidationError(file, t, { filename: base.filename, localPath })
          : getImageValidationError(file, t);
      if (validationError) {
        pushAttachmentAlert(validationError);
        items.push({
          ...base,
          status: 'error',
          error: validationError,
        });
        return items;
      }
      items.push({
        ...base,
        status: 'uploading',
      });
      return items;
    }, []);

    if (!drafts.length) return;

    setAttachments((prev) => [...prev, ...drafts]);
    drafts.forEach((draft) => {
      if (draft.status !== 'uploading') return;
      uploadAttachment(draft);
    });
  }, [attachments, pushAttachmentAlert, uploadAttachment, t]);

  const appendLocalFilePicks = useCallback((picks: LocalFilePick[]) => {
    if (!picks.length) return;

    const remaining = MAX_ATTACHMENT_COUNT - attachments.length;
    if (remaining <= 0) {
      pushAttachmentAlert(t('chat.inputAttachment.attachmentCountExceeded', { limit: MAX_ATTACHMENT_COUNT }));
      return;
    }

    const picksToAdd = picks.slice(0, remaining);
    if (picks.length > remaining) {
      pushAttachmentAlert(t('chat.inputAttachment.attachmentCountPartialAdd', { limit: MAX_ATTACHMENT_COUNT, count: remaining }));
    }

    const drafts = picksToAdd.reduce<AttachmentDraft[]>((items, pick) => {
      if (pick.error === 'forbidden') {
        pushAttachmentAlert(t('chat.inputAttachment.forbiddenFileType', { name: pick.filename }));
        return items;
      }
      if (pick.error === 'image_too_large') {
        pushAttachmentAlert(
          t('chat.inputAttachment.imageSizeExceeded', { name: pick.filename, limit: formatAttachmentSize(MAX_IMAGE_BYTES) }),
        );
        return items;
      }
      if (pick.error === 'read_failed') {
        pushAttachmentAlert(t('chat.inputAttachment.readFileFailed', { name: pick.filename }));
        return items;
      }
      if (pick.kind === 'image' && !pick.base64) {
        pushAttachmentAlert(t('chat.inputAttachment.readImageFailed', { name: pick.filename }));
        return items;
      }
      if (pick.size > MAX_FILE_BYTES) {
        pushAttachmentAlert(t('chat.inputAttachment.fileSizeExceeded', { name: pick.filename, limit: formatAttachmentSize(MAX_FILE_BYTES) }));
        return items;
      }

      const draft: AttachmentDraft = {
        id: `${pick.filename}-${pick.size}-${generateUuidV4()}`,
        kind: pick.kind,
        filename: pick.filename,
        mimeType: pick.mime_type || 'application/octet-stream',
        size: pick.size,
        localPath: pick.path,
        status: 'uploading',
        ...(pick.kind === 'image' && pick.base64
          ? {
              base64Data: pick.base64,
              previewUrl: `data:${pick.mime_type || 'application/octet-stream'};base64,${pick.base64}`,
            }
          : {}),
      };
      items.push(draft);
      return items;
    }, []);

    if (!drafts.length) return;
    setAttachments((prev) => [...prev, ...drafts]);
    drafts.forEach((draft) => {
      uploadAttachment(draft);
    });
  }, [attachments, pushAttachmentAlert, uploadAttachment, t]);

  const openAttachmentPicker = useCallback(async () => {
    if (imageInputDisabled) return;
    setAttachMenuOpen(false);
    // 文档上传依赖本机绝对路径：桌面 pywebview 或浏览器后端 path.select_files。
    // 不要回落 HTML <input type="file">，浏览器拿不到 File.path，只会得到
    // 「无法获取本地文件路径」的假失败。
    const result = await selectLocalFiles(true);
    if (result.ok) {
      appendLocalFilePicks(result.files);
      return;
    }
    if (result.reason === 'cancelled') {
      return;
    }
    const hint =
      result.reason === 'unsupported'
        ? t('chat.inputAttachment.filePickerUnsupported')
        : (result.message || t('chat.inputAttachment.filePickerFailed'));
    pushAttachmentAlert(hint);
  }, [appendLocalFilePicks, imageInputDisabled, pushAttachmentAlert, t]);

  const acceptExternalLocalFilePicks = useCallback(
    (picks: LocalFilePick[]) => {
      if (!picks.length) return;
      if (imageInputDisabled) {
        pushAttachmentAlert(t('chat.addFileDisabled'));
        return;
      }
      appendLocalFilePicks(picks);
    },
    [appendLocalFilePicks, imageInputDisabled, pushAttachmentAlert, t],
  );

  useImperativeHandle(
    ref,
    () => ({
      appendLocalFilePicks: acceptExternalLocalFilePicks,
    }),
    [acceptExternalLocalFilePicks],
  );

  useEffect(() => {
    if (!isModeMenuOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (
        !modeMenuRef.current?.contains(event.target as Node) &&
        !modeMenuPortalRef.current?.contains(event.target as Node)
      ) {
        setIsModeMenuOpen(false);
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [isModeMenuOpen]);

  useEffect(() => {
    if (!attachMenuOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (
        !attachMenuRef.current?.contains(event.target as Node) &&
        !attachMenuPortalRef.current?.contains(event.target as Node) &&
        !extensionPanelRef.current?.contains(event.target as Node) &&
        !skillPanelRef.current?.contains(event.target as Node) &&
        !swarmflowConfigPanelRef.current?.contains(event.target as Node) &&
        !swarmflowConfigBtnRef.current?.contains(event.target as Node)
      ) {
        setAttachMenuOpen(false);
        setExtensionPanelOpen(false);
        setSkillPanelOpen(false);
        setSwarmflowConfigPanelOpen(false);
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [attachMenuOpen]);

  useEffect(() => {
    if (!attachmentMenuId) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (
        target?.closest('.chat-input-attachment-menu') ||
        target?.closest('.chat-input-attachment-remove')
      ) {
        return;
      }
      setAttachmentMenuId(null);
    };

    document.addEventListener('pointerdown', handlePointerDown);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [attachmentMenuId]);

  useEffect(() => {
    if (!composerSuggestion) return;

    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        composerFrameRef.current?.contains(target) ||
        composerSuggestionMenuRef.current?.contains(target)
      ) {
        return;
      }
      setComposerSuggestion(null);
    };

    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [composerSuggestion]);

  useEffect(() => {
    if (!workMenuOpen) return;

    const handlePointerDown = (event: PointerEvent) => {
      if (!workMenuRef.current?.contains(event.target as Node)) {
        setWorkMenuOpen(null);
      }
    };
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setWorkMenuOpen(null);
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);

    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [workMenuOpen]);

  useEffect(() => {
    if (autoFocusKey) {
      inputRef.current?.focus();
    }
  }, [autoFocusKey]);

  // 切会话时用 inputValue 填充 contenteditable（chip 位置丢失，仅恢复纯文本）
  useEffect(() => {
    if (!inputRef.current) return;
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    const text = useChatStore.getState().runtimes[sid]?.inputValue ?? '';
    inputRef.current.textContent = text;
  }, [activeSessionId]);

  // 监听外部设置 inputValue 的事件（如编辑排队任务）
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { sessionId: string; value: string };
      const sid = useChatStore.getState().activeSessionId;
      if (detail.sessionId === sid && inputRef.current) {
        inputRef.current.textContent = detail.value;
        inputRef.current.focus();
        // 将光标移到末尾
        const range = document.createRange();
        range.selectNodeContents(inputRef.current);
        range.collapse(false);
        const sel = window.getSelection();
        sel?.removeAllRanges();
        sel?.addRange(range);
      }
    };
    window.addEventListener('chat-input-sync', handler);
    return () => window.removeEventListener('chat-input-sync', handler);
  }, []);

  /** 从 contenteditable 提取纯文本（技能 chip 不进入纯文本，其它 token 展开为可提交文本） */
  const extractPlainText = useCallback((): string => {
    const el = inputRef.current;
    if (!el) return '';
    let text = '';
    el.childNodes.forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent || '';
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const elem = node as HTMLElement;
        if (elem.getAttribute('contenteditable') === 'false' && elem.dataset.slashCommand) {
          text += `/${elem.dataset.slashCommand}`;
        } else if (elem.getAttribute('contenteditable') === 'false' && elem.dataset.composerToken) {
          const prefix = elem.dataset.composerToken === 'role' ? '$' : '@';
          text += `${prefix}${elem.dataset.value || elem.textContent || ''}`;
        } else if (elem.getAttribute('contenteditable') === 'false') {
          // 跳过技能 chip
        } else {
          text += elem.textContent || '';
        }
      }
    });
    return text.replace(/\u200B/g, '');
  }, []);

  /** 从 contenteditable 提取富文本（chip 转成 {{skill:名称}} 标记，保留位置用于气泡交织渲染） */
  const extractRichContent = useCallback((): string => {
    const el = inputRef.current;
    if (!el) return '';
    let text = '';
    el.childNodes.forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent || '';
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const elem = node as HTMLElement;
        if (elem.getAttribute('contenteditable') === 'false' && elem.dataset.slashCommand) {
          text += `/${elem.dataset.slashCommand}`;
        } else if (elem.getAttribute('contenteditable') === 'false' && elem.hasAttribute('data-skill')) {
          text += `{{skill:${elem.getAttribute('data-skill')}}}`;
        } else if (elem.getAttribute('contenteditable') === 'false' && elem.dataset.composerToken) {
          const prefix = elem.dataset.composerToken === 'role' ? '$' : '@';
          text += `${prefix}${elem.dataset.value || elem.textContent || ''}`;
        } else {
          text += elem.textContent || '';
        }
      }
    });
    return text.replace(/\u200B/g, '');
  }, []);

  const executeSlashCommand = useCallback(
    async (command: SlashCommand, context: SlashCommandContext, args: string) => {
      // /plan 是计划开关的命令入口。两条调用路径都汇聚到这里：
      //   1) handleSubmit 里精确输入 `/plan` 回车
      //   2) insertComposerToken 里在建议菜单选中 /plan（回车或点击）
      // 能否切换由 planModeGate 统一判断，与输入框旁的 Plan 开关、下拉菜单项、
      // 「计划」chip 关闭按钮同一套限制（会话进行中 / 等待 ask_user 回答 /
      // 有未完成目标）。命中就弹轻量提示条、消费掉这次输入，不翻转开关、
      // 不入会话历史、不发消息。
      if (command.name === 'plan') {
        const decision = evaluatePlanToggle(activeSessionId, !planActive);
        if (!decision.ok) {
          if (decision.reason) pushAttachmentAlert(t(decision.reason));
          return;
        }
      }
      if (command.name !== 'compact') {
        await command.execute(context, args);
        return;
      }

      const sessionId = context.sessionId;
      if (compactingSessionIdsRef.current.has(sessionId)) return;

      compactingSessionIdsRef.current.add(sessionId);
      setCompactingSessionIds(new Set(compactingSessionIdsRef.current));
      try {
        await command.execute(context, args);
      } finally {
        compactingSessionIdsRef.current.delete(sessionId);
        setCompactingSessionIds(new Set(compactingSessionIdsRef.current));
      }
    },
    [activeSessionId, planActive, pushAttachmentAlert, t],
  );

  const handleSubmit = useCallback(() => {
    if (composerDisabled) return;

    // 用富文本（含 chip 标记）作为发送内容，气泡可交织渲染技能
    const richContent = extractRichContent();
    const trimmedBase = (richContent + pendingVoiceText).trim();

    // 单 Agent 下拦截斜杠命令：控制命令不走 chat.send / 队列 / 中断逻辑。
    // Team 下不拦截，以普通文本发送，不会触发 command.compact 等 RPC。
    if (trimmedBase.startsWith('/')) {
      const { name, args } = parseSlashLine(trimmedBase);
      const cmd = findSlashCommand(name);
      const slashSid = useChatStore.getState().activeSessionId;
      const slashMode = useSessionStore.getState().getRuntime(slashSid)?.mode ?? mode;
      if (cmd && shouldExecuteRegisteredSlashCommand(name, args, slashMode)) {
        if (isListening) stopListening();
        if (slashSid) useChatStore.getState().setInputValue(slashSid, '');
        setPendingVoiceText('');
        setAttachments([]);
        setAttachmentAlerts([]);
        if (inputRef.current) inputRef.current.innerHTML = '';
        setComposerSuggestion(null);
        // requiresSession=false 的命令（如 /plan 纯本地开关）无需真实会话，欢迎页也能用
        if (cmd.requiresSession === false || (slashSid && slashSid !== NEW_CONVERSATION_ID)) {
          void executeSlashCommand(
            cmd,
            {
              sessionId: slashSid ?? NEW_CONVERSATION_ID,
              mode: slashMode,
              inputLine: trimmedBase,
              addMessage: useChatStore.getState().addMessage,
              submitMessage: onSubmit,
            },
            args,
          );
        } else {
          useChatStore.getState().addMessage(slashSid ?? NEW_CONVERSATION_ID, {
            id: `slash-sys-${Date.now()}`,
            role: 'system',
            content: '请先开始一个对话再使用该指令。',
            timestamp: new Date().toISOString(),
          });
        }
        return;
      }
    }
    const readyDrafts = attachments.filter(
      (attachment) =>
        attachment.status === 'ready' &&
        (Boolean(pickString(attachment.persistedMediaItem?.path)) || Boolean(attachment.base64Data)),
    );
    const trimmed = buildSubmitContent(trimmedBase, readyDrafts);
    const hasReadyMedia = readyMediaItems.length > 0;
    // Block only when there is neither text nor a ready attachment to send.
    if ((!trimmedBase && !hasReadyMedia) || hasUploadingAttachments || hasAttachmentErrors) return;
    // In agent mode attachments queue with the task (taskQueue carries mediaItems).
    // Other non-team modes still go through the text-only onInterrupt channel where
    // attachments would be lost, so keep blocking there.
    if (isInterruptible && !isTeamMode && !isAgentMode && hasReadyMedia) return;

    if (isListening) {
      stopListening();
    }

    const sid = useChatStore.getState().activeSessionId;
    if (goalArmed && trimmedBase && sid && onSetGoal && sid !== NEW_CONVERSATION_ID) {
      // command.goal carries a text objective only; silently dropping attachments
      // would make users believe they were sent, so block explicitly with an alert.
      if (readyMediaItems.length > 0) {
        pushAttachmentAlert(t('chat.goalAttachmentsBlocked'));
        return;
      }
      // command.goal 立刻发出（GoalBar「已设置」）；忙碌时用户气泡暂存，答完再入列。
      queueOrAddGoalObjectiveMessage(sid, trimmedBase);
      useGoalStore.getState().setArmed(sid, false);
      onSetGoal(sid, trimmedBase);
    } else if (goalArmed && trimmedBase && sid === NEW_CONVERSATION_ID) {
      // 欢迎页尚无真实 session，armed 状态先保留，交给 App.tsx 的 handleSendMessage
      // 在 session.create 成功、拿到真实 session id 后再落地消息 + 调 onSetGoal
      onSubmit(trimmed, readyMediaItems);
    } else if (isTeamMode) {
      onSubmit(trimmed, readyMediaItems);
    } else if (queuePaused && isAgentMode && sid) {
      // 队列已暂停时，弹窗提示用户选择
      const queueLen = useChatStore.getState().getRuntime(sid)?.taskQueue.length ?? 0;
      const shouldClear = window.confirm(t('chat.queuePausedConfirm', { count: queueLen }));
      if (shouldClear) {
        // 清空队列并发送
        useChatStore.getState().clearTaskQueue(sid);
        useChatStore.getState().setQueuePaused(sid, false);
        onSubmit(trimmed, readyMediaItems);
      } else {
        // 保持队列，新消息加入队列
        useChatStore.getState().addToTaskQueue(sid, trimmed, readyMediaItems);
      }
    } else if (isInterruptible) {
      if (isAgentMode) {
        if (sid) {
          useChatStore.getState().addToTaskQueue(sid, trimmed, readyMediaItems);
          // 目标 active 但当前没有任务在处理时，常规的自动排空触发点不会命中，主动兜底一次
          onDrainTaskQueueIfIdle?.(sid);
        }
      } else {
        onInterrupt(trimmed);
      }
    } else {
      onSubmit(trimmed, readyMediaItems);
    }
    if (sid) {
      useChatStore.getState().setInputValue(sid, '');
    }
    setPendingVoiceText('');
    setAttachments([]);
    setAttachmentAlerts([]);

    // 清空 contenteditable 内容
    if (inputRef.current) {
      inputRef.current.innerHTML = '';
    }
    setComposerSuggestion(null);
  }, [
    attachments,
    executeSlashCommand,
    extractRichContent,
    pendingVoiceText,
    readyMediaItems,
    hasUploadingAttachments,
    hasAttachmentErrors,
    composerDisabled,
    isInterruptible,
    isListening,
    onSubmit,
    onInterrupt,
    stopListening,
    mode,
    isAgentMode,
    isTeamMode,
    queuePaused,
    goalArmed,
    onSetGoal,
    onDrainTaskQueueIfIdle,
    pushAttachmentAlert,
    t,
  ]);

  const trimmedDraft = (inputValue + pendingVoiceText).trim();
  const hasTextDraft = trimmedDraft.length > 0;
  // Attachments / listening count as "composer busy" so Stop stays hidden while
  // preparing a follow-up. A pending approval keeps Stop available and blocks Send.
  const hasDraft = hasTextDraft || attachments.length > 0 || isListening;
  const isImageInterruptBlocked =
    isInterruptible && !isTeamMode && !isAgentMode && readyMediaItems.length > 0;
  const showStop = isProcessing && !isPaused && (!hasDraft || hasPendingQuestion);
  const hasReadyMedia = readyMediaItems.length > 0;
  const canSubmit = showStop || (
    !composerDisabled &&
    (hasTextDraft || hasReadyMedia) &&
    !isLoadingHistory &&
    !isImageInterruptBlocked &&
    !hasUploadingAttachments &&
    !hasAttachmentErrors
  );

  const handleSendButtonClick = useCallback(() => {
    if (showStop) {
      onCancel();
      return;
    }

    handleSubmit();
  }, [handleSubmit, showStop, onCancel]);

  const getCurrentComposerTrigger = useCallback((): ComposerSuggestionState | null => {
    const el = inputRef.current;
    const selection = window.getSelection();
    if (!el || !selection || selection.rangeCount === 0) return null;
    const range = selection.getRangeAt(0);
    if (!range.collapsed || !el.contains(range.commonAncestorContainer)) return null;

    const beforeRange = range.cloneRange();
    beforeRange.selectNodeContents(el);
    beforeRange.setEnd(range.endContainer, range.endOffset);
    const beforeText = beforeRange.toString().replace(/\u200B/g, '');
    const slashMatch = beforeText.match(/(?:^|\s)(\/)([a-zA-Z][\w-]*)?$/);
    if (slashMatch) {
      return { kind: 'slash', query: slashMatch[2] ?? '' };
    }
    const match = beforeText.match(/([@$])([\p{L}\p{N}_\-\u4e00-\u9fa5]*)$/u);
    if (!match) return null;

    return {
      kind: match[1] === '@' ? 'member' : 'role',
      query: match[2] || '',
    };
  }, []);

  const updateComposerSuggestion = useCallback(() => {
    const trigger = getCurrentComposerTrigger();
    if (!trigger) {
      setComposerSuggestion(null);
      return;
    }
    // slash 指令不依赖团队成员，即便没有可 @ 的成员也照常弹出
    if (trigger.kind !== 'slash' && mentionableMembers.length === 0) {
      setComposerSuggestion(null);
      return;
    }
    setComposerSuggestion(trigger);
  }, [getCurrentComposerTrigger, mentionableMembers.length]);

  const setRangeStartByTextOffset = useCallback((range: Range, root: HTMLElement, offset: number) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let consumed = 0;
    let node = walker.nextNode();
    while (node) {
      const text = (node.textContent || '').replace(/\u200B/g, '');
      const next = consumed + text.length;
      if (offset <= next) {
        range.setStart(node, Math.max(0, offset - consumed));
        return;
      }
      consumed = next;
      node = walker.nextNode();
    }
    range.selectNodeContents(root);
    range.collapse(false);
  }, []);

  const insertComposerToken = useCallback((
    kind: ComposerSuggestionKind,
    value: string,
    label: string,
    slashItemKind?: 'command' | 'skill',
    slashTakesArgs?: boolean,
  ) => {
    const el = inputRef.current;
    const selection = window.getSelection();
    if (!el || !selection || selection.rangeCount === 0) return;
    const range = selection.getRangeAt(0);
    if (!el.contains(range.commonAncestorContainer)) return;

    // slash 选中
    if (kind === 'slash') {
      if (slashItemKind === 'skill') {
        const trigger = getCurrentComposerTrigger();
        if (trigger) {
          const beforeRange = range.cloneRange();
          beforeRange.selectNodeContents(el);
          beforeRange.setEnd(range.endContainer, range.endOffset);
          const beforeTextLength = beforeRange.toString().replace(/​/g, '').length;
          const triggerLength = trigger.query.length + 1;
          setRangeStartByTextOffset(range, el, Math.max(0, beforeTextLength - triggerLength));
          range.deleteContents();
        }
        savedRangeRef.current = range.cloneRange();
        const slashSid = useChatStore.getState().activeSessionId;
        if (slashSid) useSessionStore.getState().addSelectedSkill(slashSid, value);
        insertSkillChipRef.current(value);
        setComposerSuggestion(null);
        return;
      }
      const slashSid = useChatStore.getState().activeSessionId;
      const slashMode = useSessionStore.getState().getRuntime(slashSid)?.mode ?? mode;
      if (!supportsWebSlashCommands(slashMode)) {
        setComposerSuggestion(null);
        return;
      }
      const slashCmd = findSlashCommand(value);
      // 无参命令（/plan、/compact）：选中即执行，不插入文本、不再等回车。
      // `/plan hi` 这类手工输入不走此选中路径，提交时会被当作普通消息。
      if (slashCmd && slashTakesArgs === false) {
        const trigger = getCurrentComposerTrigger();
        if (trigger) {
          const beforeRange = range.cloneRange();
          beforeRange.selectNodeContents(el);
          beforeRange.setEnd(range.endContainer, range.endOffset);
          const beforeTextLength = beforeRange.toString().replace(/​/g, '').length;
          const triggerLength = trigger.query.length + 1; // '/' + query
          setRangeStartByTextOffset(range, el, Math.max(0, beforeTextLength - triggerLength));
          range.deleteContents();
        }
        if (slashSid) useChatStore.getState().setInputValue(slashSid, extractPlainText());
        setComposerSuggestion(null);
        el.focus();
        // requiresSession=false 的命令（如 /plan 纯本地开关）无需真实会话，欢迎页也能用
        if (slashCmd.requiresSession === false || (slashSid && slashSid !== NEW_CONVERSATION_ID)) {
          void executeSlashCommand(
            slashCmd,
            {
              sessionId: slashSid ?? NEW_CONVERSATION_ID,
              mode: slashMode,
              inputLine: `/${value}`,
              addMessage: useChatStore.getState().addMessage,
              submitMessage: onSubmit,
            },
            '',
          );
        } else {
          useChatStore.getState().addMessage(slashSid ?? NEW_CONVERSATION_ID, {
            id: `slash-sys-${Date.now()}`,
            role: 'system',
            content: '请先开始一个对话再使用该指令。',
            timestamp: new Date().toISOString(),
          });
        }
        return;
      }
      // 有参命令（如 /persist）：把 "/query" 替换成蓝色原子 chip。提取文本时再还原为
      // "/<name>"，因此视觉表现和技能一致，同时保留既有命令解析/提交语义。
      const trigger = getCurrentComposerTrigger();
      if (trigger) {
        const beforeRange = range.cloneRange();
        beforeRange.selectNodeContents(el);
        beforeRange.setEnd(range.endContainer, range.endOffset);
        const beforeTextLength = beforeRange.toString().replace(/​/g, '').length;
        const triggerLength = trigger.query.length + 1; // '/' + query
        setRangeStartByTextOffset(range, el, Math.max(0, beforeTextLength - triggerLength));
        range.deleteContents();
      }
      const chip = document.createElement('span');
      chip.className = 'chat-input-chip-inline chat-input-chip-inline--slash-command';
      chip.setAttribute('contenteditable', 'false');
      chip.dataset.slashCommand = value;

      const prefix = document.createElement('span');
      prefix.className = 'chat-input-chip-inline__prefix';
      prefix.textContent = '/';

      const labelEl = document.createElement('span');
      labelEl.className = 'chat-input-chip-inline__label';
      labelEl.textContent = label.replace(/^\/+/, '');

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'chat-input-chip-inline__remove';
      removeBtn.setAttribute('aria-label', 'remove slash command');
      removeBtn.innerHTML = `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.4"><path stroke-linecap="round" stroke-linejoin="round" d="M6 6l8 8M14 6l-8 8"/></svg>`;
      removeBtn.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        const next = chip.nextSibling;
        if (next && next.nodeType === Node.TEXT_NODE) {
          const nextText = next.textContent || '';
          if (nextText.startsWith(' ')) {
            next.textContent = nextText.slice(1);
          }
        }
        chip.remove();
        const sid = useChatStore.getState().activeSessionId;
        if (sid) useChatStore.getState().setInputValue(sid, extractPlainText());
      });

      chip.append(prefix, labelEl, removeBtn);
      range.insertNode(chip);
      const spacer = document.createTextNode(' ');
      chip.after(spacer);
      range.setStartAfter(spacer);
      range.setEndAfter(spacer);
      selection.removeAllRanges();
      selection.addRange(range);
      const sid = useChatStore.getState().activeSessionId;
      if (sid) useChatStore.getState().setInputValue(sid, extractPlainText());
      setComposerSuggestion(null);
      el.focus();
      return;
    }

    const trigger = getCurrentComposerTrigger();
    if (trigger) {
      const beforeRange = range.cloneRange();
      beforeRange.selectNodeContents(el);
      beforeRange.setEnd(range.endContainer, range.endOffset);
      const beforeTextLength = beforeRange.toString().replace(/\u200B/g, '').length;
      const triggerLength = trigger.query.length + 1;
      setRangeStartByTextOffset(range, el, Math.max(0, beforeTextLength - triggerLength));
      range.deleteContents();
    }

    const chip = document.createElement('span');
    chip.className = `chat-input-chip-inline chat-input-chip-inline--${kind}`;
    chip.setAttribute('contenteditable', 'false');
    chip.dataset.composerToken = kind;
    chip.dataset.value = value;

    const prefix = document.createElement('span');
    prefix.className = 'chat-input-chip-inline__prefix';
    prefix.textContent = kind === 'role' ? '$' : '@';

    const labelEl = document.createElement('span');
    labelEl.className = 'chat-input-chip-inline__label';
    labelEl.textContent = label;

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'chat-input-chip-inline__remove';
    removeBtn.setAttribute('aria-label', kind === 'role' ? 'remove role' : 'remove member');
    removeBtn.innerHTML = `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.4"><path stroke-linecap="round" stroke-linejoin="round" d="M6 6l8 8M14 6l-8 8"/></svg>`;
    removeBtn.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      const next = chip.nextSibling;
      if (next && next.nodeType === Node.TEXT_NODE) {
        const nextText = next.textContent || '';
        if (nextText === '\u200B') {
          next.remove();
        } else if (nextText.startsWith(' ')) {
          next.textContent = nextText.slice(1);
        }
      }
      chip.remove();
      const sid = useChatStore.getState().activeSessionId;
      if (sid) useChatStore.getState().setInputValue(sid, extractPlainText());
    });

    chip.append(prefix, labelEl, removeBtn);
    range.insertNode(chip);

    const spacer = document.createTextNode(' ');
    chip.after(spacer);
    range.setStartAfter(spacer);
    range.setEndAfter(spacer);
    selection.removeAllRanges();
    selection.addRange(range);

    const sid = useChatStore.getState().activeSessionId;
    if (sid) useChatStore.getState().setInputValue(sid, extractPlainText());
    setComposerSuggestion(null);
    el.focus();
  }, [executeSlashCommand, extractPlainText, getCurrentComposerTrigger, mode, onSubmit, setRangeStartByTextOffset]);

  const notifyKVCInputIntent = useCallback(() => {
    if (!activeSessionId || activeSessionId === NEW_CONVERSATION_ID) return;
    onInputIntent?.(activeSessionId);
  }, [activeSessionId, onInputIntent]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      // keydown is the most reliable signal in the current Web frontend. Keep
      // beforeinput/input/paste below as IME and WebView compatibility paths.
      const isPrintableKey = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
      const isPasteShortcut = (e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v';
      if (isPrintableKey || isPasteShortcut) {
        notifyKVCInputIntent();
      }
      if (composerSuggestion) {
        if (e.key === 'Escape') {
          e.preventDefault();
          setComposerSuggestion(null);
          return;
        }

        if (e.key === 'ArrowDown') {
          e.preventDefault();
          setComposerSuggestionNavigationMode('keyboard');
          moveComposerSuggestionHighlight(1);
          return;
        }

        if (e.key === 'ArrowUp') {
          e.preventDefault();
          setComposerSuggestionNavigationMode('keyboard');
          moveComposerSuggestionHighlight(-1);
          return;
        }

        if ((e.key === 'Enter' || e.key === 'Tab') && !e.shiftKey) {
          if (isImeCompositionKey(e.nativeEvent, isComposingRef.current)) return;
          e.preventDefault();
          const item = composerSuggestionItems[composerSuggestionIndex];
          if (item && !item.disabled) {
            insertComposerToken(
              composerSuggestion.kind,
              item.id,
              item.label,
              item.itemKind,
              item.takesArgs,
            );
          }
          return;
        }
      }

      if (e.key !== 'Enter' || e.shiftKey) return;
      if (isImeCompositionKey(e.nativeEvent, isComposingRef.current)) return;
      e.preventDefault();
      handleSubmit();
    },
    [
      composerSuggestion,
      composerSuggestionIndex,
      composerSuggestionItems,
      handleSubmit,
      insertComposerToken,
      moveComposerSuggestionHighlight,
      notifyKVCInputIntent,
    ]
  );

  /**
   * Start KVC preparation on the leading edge of a real editor insertion.
   * `onInput` remains below as a compatibility fallback for WebViews that do
   * not expose a useful beforeinput event.
   */
  const handleEditorBeforeInput = useCallback(
    (event: FormEvent<HTMLDivElement>) => {
      const nativeEvent = event.nativeEvent as InputEvent;
      if (String(nativeEvent.inputType || '').startsWith('insert')) {
        notifyKVCInputIntent();
      }
    },
    [notifyKVCInputIntent],
  );

  /** contenteditable 输入时同步纯文本到 store + 联动 selectedSkills */
  const handleEditorInput = useCallback(() => {
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    // 提取纯文本
    const text = extractPlainText();
    useChatStore.getState().setInputValue(sid, text);
    if (text.trim() && sid !== NEW_CONVERSATION_ID) {
      notifyKVCInputIntent();
    }
    // 联动 selectedSkills：扫描 contenteditable 现有 chip，移除已不在的技能（backspace 删除等情况）
    const el = inputRef.current;
    if (el) {
      const existingSkills = new Set<string>();
      el.querySelectorAll('[data-skill]').forEach((chip) => {
        const name = chip.getAttribute('data-skill');
        if (name) existingSkills.add(name);
      });
      const store = useSessionStore.getState();
      const current = store.runtimes[sid]?.selectedSkills ?? [];
      current.forEach((skill) => {
        if (!existingSkills.has(skill)) {
          store.removeSelectedSkill(sid, skill);
        }
      });
    }
    updateComposerSuggestion();
  }, [extractPlainText, notifyKVCInputIntent, updateComposerSuggestion]);

  /** 保存当前光标位置（用于技能插入时定位） */
  const saveSelection = useCallback(() => {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    // 仅当光标在 contenteditable 内时保存
    if (inputRef.current && inputRef.current.contains(range.commonAncestorContainer)) {
      savedRangeRef.current = range.cloneRange();
    }
  }, []);

  const handleFileInputChange = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (files) {
      void appendAttachmentFiles(files);
    }
    event.target.value = '';
  }, [appendAttachmentFiles]);

  const handleDesktopFilePaste = useCallback(
    (event: ClipboardEvent | globalThis.ClipboardEvent) => {
      if (!isDesktopBridgeReady && !isDesktopLocalFilePicker()) return false;

      const target = event.target as Node | null;
      const shell = inputRef.current?.closest('.chat-panel-shell');
      if (!shell || !target || !shell.contains(target)) return false;

      const hasBrowserFiles = clipboardHasFileItems(event.clipboardData);
      // Capture File blobs before any await; clipboardData can become unavailable.
      const imageFiles = hasBrowserFiles ? getClipboardImageFiles(event.clipboardData) : [];

      if (hasBrowserFiles) {
        event.preventDefault();
        if (imageInputDisabled) {
          if (shouldAlertImagePasteDisabled(true, imageFiles.length > 0)) {
            pushAttachmentAlert(t(IMAGE_INPUT_DISABLED_ALERT_KEY));
          }
          return true;
        }
        void (async () => {
          const clipboardPicks = await getClipboardFilePicks();
          if (clipboardPicks.length) {
            appendLocalFilePicks(clipboardPicks);
            return;
          }
          if (imageFiles.length) {
            appendAttachmentFiles(imageFiles);
          }
        })();
        return true;
      }

      // Explorer-copied files may only expose CF_HDROP to the native bridge.
      // Do not block text paste; append native file picks if any are found.
      if (!imageInputDisabled) {
        void (async () => {
          const clipboardPicks = await getClipboardFilePicks();
          if (clipboardPicks.length) {
            appendLocalFilePicks(clipboardPicks);
          }
        })();
      }
      return false;
    },
    [
      appendAttachmentFiles,
      appendLocalFilePicks,
      imageInputDisabled,
      isDesktopBridgeReady,
      pushAttachmentAlert,
      t,
    ],
  );

  const handlePaste = useCallback(
    (event: ClipboardEvent<HTMLDivElement>) => {
      const hasText = Boolean(event.clipboardData.getData('text/plain').trim());
      if (hasText) {
        notifyKVCInputIntent();
      }
      if (handleDesktopFilePaste(event)) return;

      const imageFiles = getClipboardImageFiles(event.clipboardData);
      if (imageFiles.length && !hasText) {
        event.preventDefault();
        if (shouldAlertImagePasteDisabled(imageInputDisabled, true)) {
          pushAttachmentAlert(t(IMAGE_INPUT_DISABLED_ALERT_KEY));
          return;
        }
        appendAttachmentFiles(imageFiles);
        return;
      }

      if (clipboardHasFileItems(event.clipboardData) && !hasText) {
        event.preventDefault();
      }
    },
    [appendAttachmentFiles, handleDesktopFilePaste, imageInputDisabled, notifyKVCInputIntent, pushAttachmentAlert, t],
  );

  useEffect(() => {
    if (!isDesktopBridgeReady) return undefined;

    const onDocumentPaste = (event: globalThis.ClipboardEvent) => {
      // contenteditable onPaste already covers the composer; this covers the rest of the shell.
      if (inputRef.current?.contains(event.target as Node)) return;
      handleDesktopFilePaste(event);
    };

    document.addEventListener('paste', onDocumentPaste);
    return () => document.removeEventListener('paste', onDocumentPaste);
  }, [handleDesktopFilePaste, isDesktopBridgeReady]);

  const handleFileDragOver = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    // Never set dropEffect='none'/'move' inside the desktop shell — WebView2
    // rejects those for Explorer file drags and shows the forbidden cursor.
    const desktop = isDesktopBridgeReady || isDesktopShell() || isDesktopLocalFilePicker();
    if (desktop) {
      event.dataTransfer.dropEffect = 'copy';
      return;
    }
    // Browser / whl: reject OS file drops (no absolute path bridge).
    event.dataTransfer.dropEffect = 'none';
  }, [isDesktopBridgeReady]);

  const handleFileDrop = useCallback((event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    // Desktop paths arrive via jiuwen-desktop-local-files from pywebview.
  }, []);

  /** 在光标处插入技能 chip（不可编辑原子节点） */
  const insertSkillChip = useCallback((skillName: string) => {
    const el = inputRef.current;
    if (!el) return;
    // 输入法合成中不插入
    if (isComposingRef.current) return;

    el.focus();
    const sel = window.getSelection();
    if (!sel) return;

    // 恢复保存的光标，否则用当前光标
    let range: Range;
    if (savedRangeRef.current && el.contains(savedRangeRef.current.commonAncestorContainer)) {
      range = savedRangeRef.current;
      sel.removeAllRanges();
      sel.addRange(range);
    } else if (sel.rangeCount > 0) {
      range = sel.getRangeAt(0);
    } else {
      // 无光标，追加到末尾
      range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
    }

    // 删除选中的内容（如有）
    range.deleteContents();

    // 创建 chip 节点
    const chip = document.createElement('span');
    chip.className = 'chat-input-chip-inline';
    chip.setAttribute('contenteditable', 'false');
    chip.setAttribute('data-skill', skillName);
    chip.innerHTML = `
      <span class="chat-input-chip-inline__icon" aria-hidden="true"></span>
      <span class="chat-input-chip-inline__label">${skillName}</span>
    `;
    // 删除按钮（覆盖在 icon 位置，悬浮时替换闪电）
    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'chat-input-chip-inline__remove';
    removeBtn.setAttribute('aria-label', 'remove skill');
    removeBtn.innerHTML = `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2.4"><path stroke-linecap="round" stroke-linejoin="round" d="M6 6l8 8M14 6l-8 8"/></svg>`;
    removeBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const sid = useChatStore.getState().activeSessionId;
      // 从 DOM 移除 chip
      chip.remove();
      // 同步 selectedSkills
      if (sid) useSessionStore.getState().removeSelectedSkill(sid, skillName);
      // 同步纯文本
      if (sid) useChatStore.getState().setInputValue(sid, extractPlainText());
    });
    // 把 remove 按钮插入到 icon 容器内（覆盖闪电位置）
    const iconEl = chip.querySelector('.chat-input-chip-inline__icon');
    if (iconEl) {
      iconEl.appendChild(removeBtn);
    } else {
      chip.appendChild(removeBtn);
    }

    // 插入 chip
    range.insertNode(chip);

    // 在 chip 后插入零宽空格，方便光标定位
    const spacer = document.createTextNode('\u200B');
    chip.after(spacer);

    // 光标移到 spacer 后
    range.setStartAfter(spacer);
    range.setEndAfter(spacer);
    sel.removeAllRanges();
    sel.addRange(range);

    // 清除保存的光标
    savedRangeRef.current = null;

    // 同步纯文本到 store
    const sid = useChatStore.getState().activeSessionId;
    if (sid) useChatStore.getState().setInputValue(sid, extractPlainText());
  }, [extractPlainText]);

  useEffect(() => {
    insertSkillChipRef.current = insertSkillChip;
  }, [insertSkillChip]);

  /** 从 contenteditable 中移除指定技能的 chip 节点 */
  const removeSkillChip = useCallback((skillName: string) => {
    const el = inputRef.current;
    if (!el) return;
    const chips = el.querySelectorAll('[data-skill]');
    chips.forEach((chip) => {
      if (chip.getAttribute('data-skill') === skillName) {
        // 同时移除后面的零宽空格 spacer
        const next = chip.nextSibling;
        if (next && next.nodeType === Node.TEXT_NODE && next.textContent === '\u200B') {
          next.remove();
        }
        chip.remove();
      }
    });
    // 同步纯文本
    const sid = useChatStore.getState().activeSessionId;
    if (sid) useChatStore.getState().setInputValue(sid, extractPlainText());
  }, [extractPlainText]);

  // 监听从 SkillPanel 发来的"跳转到聊天并插入技能"事件
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { skillName: string; prefixText?: string; suffixText?: string; secondSkillName?: string };
      const sid = useChatStore.getState().activeSessionId;
      if (!sid || !inputRef.current) return;

      // 清空输入框并插入前缀文本（如"帮我修改这个技能"）
      inputRef.current.textContent = detail.prefixText || '';
      inputRef.current.focus();

      // 将光标移到末尾，确保技能 chip 插入在前缀文本之后
      const range = document.createRange();
      range.selectNodeContents(inputRef.current);
      range.collapse(false);
      const sel = window.getSelection();
      sel?.removeAllRanges();
      sel?.addRange(range);

      // 先更新 store 中的 selectedSkills，再插入 chip DOM
      useSessionStore.getState().addSelectedSkill(sid, detail.skillName);
      insertSkillChip(detail.skillName);

      // 如果有后缀文本（如"帮我修改这个技能"），追加到 chip 之后
      if (detail.suffixText) {
        inputRef.current.appendChild(document.createTextNode(detail.suffixText));
        // 光标移到末尾
        const r = document.createRange();
        r.selectNodeContents(inputRef.current);
        r.collapse(false);
        const s = window.getSelection();
        s?.removeAllRanges();
        s?.addRange(r);
      }

      // 如果有第二个技能（如被编辑的技能），追加 chip
      if (detail.secondSkillName) {
        useSessionStore.getState().addSelectedSkill(sid, detail.secondSkillName);
        insertSkillChip(detail.secondSkillName);
      }

      // 同步纯文本到 store（chip 不进入纯文本，前缀/后缀文本会保留）
      useChatStore.getState().setInputValue(sid, extractPlainText());
    };
    window.addEventListener('chat-input-insert-skill', handler);
    return () => window.removeEventListener('chat-input-insert-skill', handler);
  }, [insertSkillChip, extractPlainText]);
  // 外部进入新会话时可以预选技能。把 canonical session state 同步成输入框
  // 中的 chip，避免用户开始编辑后被 handleEditorInput 误判为手动移除。
  useEffect(() => {
    const el = inputRef.current;
    if (!el || !activeSessionId || selectedSkills.length === 0) return;
    const existing = new Set(
      Array.from(el.querySelectorAll('[data-skill]'))
        .map((node) => node.getAttribute('data-skill'))
        .filter((name): name is string => Boolean(name)),
    );
    selectedSkills.forEach((skill) => {
      if (!existing.has(skill)) insertSkillChip(skill);
    });
  }, [activeSessionId, insertSkillChip, selectedSkills]);

  // const handleVoiceStart = useCallback(() => {
  //   if (isListening) return;
  //   stopAllTts();
  //   startListening();
  // }, [isListening, startListening]);

  // const handleVoiceEnd = useCallback(() => {
  //   if (!isListening) return;
  //   stopListening();
  // }, [isListening, stopListening]);

  // const handleVoicePointerDown = useCallback(
  //   (e: ReactPointerEvent<HTMLButtonElement>) => {
  //     // 仅响应主按钮按压，避免右键/多指导致状态抖动
  //     if (e.pointerType === 'mouse' && e.button !== 0) return;
  //     if (activePointerIdRef.current !== null) return;
  //     e.preventDefault();
  //     activePointerIdRef.current = e.pointerId;
  //     isVoicePressingRef.current = true;
  //     e.currentTarget.setPointerCapture(e.pointerId);
  //     handleVoiceStart();
  //   },
  //   [handleVoiceStart]
  // );

  // const handleVoicePointerUp = useCallback(
  //   (e: ReactPointerEvent<HTMLButtonElement>) => {
  //     if (activePointerIdRef.current !== e.pointerId) return;
  //     e.preventDefault();
  //     activePointerIdRef.current = null;
  //     isVoicePressingRef.current = false;
  //     if (e.currentTarget.hasPointerCapture(e.pointerId)) {
  //       e.currentTarget.releasePointerCapture(e.pointerId);
  //     }
  //     handleVoiceEnd();
  //   },
  //   [handleVoiceEnd]
  // );

  // const handleVoicePointerCancel = useCallback(
  //   (e: ReactPointerEvent<HTMLButtonElement>) => {
  //     if (activePointerIdRef.current !== e.pointerId) return;
  //     activePointerIdRef.current = null;
  //     isVoicePressingRef.current = false;
  //     if (e.currentTarget.hasPointerCapture(e.pointerId)) {
  //       e.currentTarget.releasePointerCapture(e.pointerId);
  //     }
  //     handleVoiceEnd();
  //   },
  //   [handleVoiceEnd]
  // );

  const handleModeSwitch = useCallback(async (targetMode: AgentMode) => {
    if (isProcessing || hasHistory || mode === targetMode) return;
    onSwitchMode(targetMode);
  }, [isProcessing, hasHistory, mode, onSwitchMode]);

  const handleModeSelect = useCallback(async (targetMode: AgentMode) => {
    setIsModeMenuOpen(false);
    setHoveredOptionDesc(null);
    setHoveredOptionRect(null);
    await handleModeSwitch(targetMode);
  }, [handleModeSwitch]);

  useEffect(() => {
    setIsModeMenuOpen(false);
  }, [isProcessing, mode]);

  useEffect(() => {
    if (!isModeMenuOpen) {
      setHoveredOptionDesc(null);
      setHoveredOptionRect(null);
    }
  }, [isModeMenuOpen]);

  const openProjectCreateDialog = useCallback(async (mode: ProjectCreateMode) => {
    setProjectDirError(null);
    setProjectCreateMode(mode);
    setWorkMenuOpen(null);

    if (mode === 'blank') {
      setProjectNameDraft('');
      setProjectDirDraft('');
      setWorkDialogOpen(true);
      return;
    }

    if (!isProjectDirectoryPickerSupported()) {
      setProjectNameDraft('');
      setProjectDirDraft('');
      setWorkDialogOpen(true);
      return;
    }

    const result = await selectProjectDirectory();
    if (!result.ok) {
      if (result.reason !== 'cancelled') {
        setProjectNameDraft('');
        setProjectDirDraft('');
        setWorkDialogOpen(true);
        setProjectDirError(
          result.reason === 'unsupported'
            ? t('multiSession.project.directoryPickerUnsupported')
            : result.message || t('multiSession.project.directoryPickerFailed'),
        );
      }
      return;
    }

    try {
      await createProject(result.name, result.path);
    } catch (error) {
      const errorKey = projectCreateErrorKey(error);
      setProjectDirError(errorKey ? t(errorKey) : error instanceof Error ? error.message : String(error));
    }
  }, [createProject, t]);

  const handleAddProjectDir = useCallback(async () => {
    const name = projectNameDraft.trim();
    const projectDir = projectCreateMode === 'blank' ? '' : projectDirDraft.trim();
    if (!name || (projectCreateMode === 'existing' && !projectDir)) return;
    setProjectDirError(null);
    if (projectDir && (!isLikelyAbsolutePath(projectDir) || projectDir.startsWith('~/'))) {
      setProjectDirError(t('multiSession.project.absolutePathError'));
      return;
    }
    try {
      await createProject(name, projectDir);
      setProjectNameDraft('');
      setProjectDirDraft('');
      setWorkDialogOpen(false);
    } catch (error) {
      const errorKey = projectCreateErrorKey(error);
      setProjectDirError(errorKey ? t(errorKey) : error instanceof Error ? error.message : String(error));
    }
  }, [createProject, projectCreateMode, projectNameDraft, projectDirDraft, t]);

  const currentMode = AGENT_MODE_OPTIONS.find((item) => item.value === mode) ?? AGENT_MODE_OPTIONS[0];
  const evolutionLabel = getEvolutionPillLabel(mode, evolutionStatus, t);
  const attachmentAlertPortalTarget = inputRef.current?.closest<HTMLElement>('.chat-panel-shell');
  const showSlashSuggestionBelow = (
    showWorkContextRow && composerSuggestion?.kind === 'slash'
  );

  return (
    <>
      {attachmentAlerts.length > 0 && attachmentAlertPortalTarget && createPortal(
        <div className="chat-input-local-alerts" role="status" aria-live="polite" data-testid="chat-panel-input-local-alerts">
          {attachmentAlerts.map((alert) => (
            <div className="chat-input-local-alert" key={alert.id} data-testid="chat-panel-input-local-alert" data-variant={alert.id}>
              <CircleX size={16} strokeWidth={2.2} aria-hidden="true" />
              <span>{alert.message}</span>
              <button
                type="button"
                data-testid="chat-panel-input-local-alert-dismiss"
                onClick={() => dismissAttachmentAlert(alert.id)}
                aria-label={t('common.close')}
              >
                <X size={15} strokeWidth={2} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>,
        attachmentAlertPortalTarget,
      )}
      <div ref={composerFrameRef} className="chat-input-frame" data-testid="chat-panel-input-frame">
        {isCompactRunning && (
          <div
            className="chat-input-compact-progress"
            role="status"
            aria-live="polite"
            data-testid="chat-panel-input-compact-progress"
          >
            <Loader2
              className="chat-input-compact-progress__spinner"
              size={16}
              strokeWidth={1.8}
              aria-hidden="true"
            />
            <span>{t('chat.contextCompressionCommandRunning')}</span>
          </div>
        )}
        <div
          className={cx(
            'chat-input-container',
            showWorkContextRow && 'chat-input-container--work-home',
            (isModeMenuOpen || workMenuOpen) && 'chat-input-container--menu-open',
            composerSuggestion && !showSlashSuggestionBelow && 'chat-input-container--suggestion-open',
            isListening && 'chat-input-container--recording',
            isCompactRunning && 'chat-input-container--command-pending',
          )}
          data-testid="chat-panel-input-container"
          onDragOver={handleFileDragOver}
          onDrop={handleFileDrop}
        >
      {isListening && (
        <div className="chat-input-recording-bar" data-testid="chat-panel-input-recording-bar">
          <span className="chat-input-recording-dot" />
          <span>{t('chat.recording')}</span>
        </div>
      )}

      <div className="chat-input-body" data-testid="chat-panel-input-body">
      {attachments.length > 0 && (
        <div className="chat-input-attachment-panel" data-testid="chat-panel-input-attachment-panel">
          <div
            className={cx(
              'chat-input-attachment-grid',
              attachmentMenuId && 'chat-input-attachment-grid--menu-open',
            )}
            data-testid="chat-panel-input-attachment-grid"
          >
            {attachments.map((attachment) => (
              <div
                className={cx(
                  'chat-input-attachment-card',
                  attachment.status === 'error' && 'chat-input-attachment-card--error',
                  attachment.status === 'uploading' && 'chat-input-attachment-card--uploading',
                )}
                key={attachment.id}
                data-testid="chat-panel-input-attachment-card"
                data-variant={attachment.id}
              >
                <div
                  className={cx(
                    'chat-input-attachment-preview',
                    attachment.previewUrl && 'chat-input-attachment-preview--image',
                  )}
                  aria-hidden="true"
                  data-testid="chat-panel-input-attachment-preview"
                >
                  {attachment.previewUrl ? (
                    <img src={attachment.previewUrl} alt="" />
                  ) : (
                    <FileIcon fileName={attachment.filename} size={32} />
                  )}
                </div>
                <div className="chat-input-attachment-main" data-testid="chat-panel-input-attachment-main">
                  <div className="chat-input-attachment-name" title={attachment.filename} data-testid="chat-panel-input-attachment-name">
                    {attachment.filename}
                  </div>
                  <div className="chat-input-attachment-meta" data-testid="chat-panel-input-attachment-meta">
                    {attachment.status === 'uploading' ? (
                      <>
                        <Loader2 className="chat-input-attachment-spin" size={12} strokeWidth={2} />
                        <span data-testid="chat-panel-input-attachment-status" data-variant="uploading">{t('chat.uploading')}</span>
                      </>
                    ) : attachment.status === 'error' ? (
                      <>
                        <span
                          className="chat-input-attachment-status-error"
                          data-testid="chat-panel-input-attachment-status"
                          data-variant="error"
                          title={attachment.error || t('chat.uploadFailed')}
                        >
                          {t('chat.uploadFailed')}
                        </span>
                        {attachment.file && (
                          <button
                            type="button"
                            className="chat-input-attachment-retry"
                            data-testid="chat-panel-input-attachment-retry"
                            onClick={() => retryAttachment(attachment)}
                          >
                            {t('chat.retry')}
                          </button>
                        )}
                      </>
                    ) : (
                      <>
                        <span>
                          {attachment.kind === 'document'
                            ? (getFileExtension(attachment.filename).replace('.', '').toUpperCase() || 'FILE')
                            : (attachment.mimeType.split('/')[1]?.toUpperCase() || 'IMAGE')}
                        </span>
                        <span>{formatAttachmentSize(attachment.size)}</span>
                      </>
                    )}
                  </div>
                </div>
                <button
                  type="button"
                  className="chat-input-attachment-remove"
                  data-testid="chat-panel-input-attachment-remove"
                  onPointerDown={(e) => startAttachmentMenuTimer(attachment.id, e.currentTarget)}
                  onPointerUp={stopAttachmentMenuTimer}
                  onPointerCancel={stopAttachmentMenuTimer}
                  onPointerLeave={stopAttachmentMenuTimer}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    stopAttachmentMenuTimer();
                    setAttachmentMenuAnchor(event.currentTarget.getBoundingClientRect());
                    setAttachmentMenuId(attachment.id);
                  }}
                  onClick={() => handleAttachmentRemoveClick(attachment.id)}
                  title={t('chat.deleteLongPress')}
                  aria-label={t('chat.deleteAttachment')}
                >
                  <X size={12} strokeWidth={2} />
                </button>
                {attachmentMenuId === attachment.id && attachmentMenuAnchor && createPortal(
                  <div
                    className="chat-input-attachment-menu"
                    role="menu"
                    data-testid="chat-panel-input-attachment-menu"
                    style={{
                      position: 'fixed',
                      top: attachmentMenuAnchor.top,
                      left: attachmentMenuAnchor.right + 4,
                      zIndex: 9999,
                    }}
                  >
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="chat-panel-input-attachment-menu-delete"
                      onClick={() => removeAttachment(attachment.id)}
                    >
                      {t('chat.delete')}
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="chat-panel-input-attachment-menu-clear"
                      onClick={clearAttachments}
                    >
                      {t('chat.clearAttachments')}
                    </button>
                  </div>,
                  document.body
                )}
              </div>
            ))}
          </div>
        </div>
      )}
      {composerSuggestion && !showSlashSuggestionBelow && (
        <ComposerSuggestionMenu
          suggestion={composerSuggestion}
          items={composerSuggestionItems}
          highlightedIndex={composerSuggestionIndex}
          navigationMode={composerSuggestionNavigationMode}
          containerRef={composerSuggestionMenuRef}
          onPointerHighlight={(index) => {
            setComposerSuggestionNavigationMode('pointer');
            setComposerSuggestionIndex(index);
          }}
          onPick={insertComposerToken}
          loading={slashCatalogLoading}
          slashSkillsOnly={isTeamMode}
        />
      )}
      <div
        ref={inputRef}
        contentEditable={!composerDisabled}
        aria-disabled={composerDisabled}
        suppressContentEditableWarning
        onBeforeInput={handleEditorBeforeInput}
        onInput={handleEditorInput}
        onKeyDown={handleKeyDown}
        onCompositionStart={() => { isComposingRef.current = true; }}
        onCompositionEnd={() => { isComposingRef.current = false; }}
        onBlur={saveSelection}
        onPaste={handlePaste}
        data-placeholder={
          isCompactRunning
            ? t('chat.placeholderCompacting')
            : hasPendingQuestion
              ? t('chat.placeholderAwaitingApproval')
            : isListening
              ? t('chat.placeholderVoice')
            : isTeamMode
              ? isInterruptible && !isPaused
              ? t('chat.placeholderTeamModeProcessing')
              : t('chat.placeholderTeamMode')
              : isAutoHarnessMode
                ? t('autoHarness.inputPlaceholder')
                : isAgentMode && isInterruptible
                  ? t('chat.placeholderProcessingQueue')
                  : isInterruptible
                    ? t('chat.placeholderProcessing')
                    : t('chat.placeholder')
        }
        className={cx('chat-input-editor', composerDisabled && 'chat-input-editor--disabled')}
        data-testid="chat-panel-input"
      />

      <div className="chat-input-toolbar" data-testid="chat-panel-input-toolbar">
        <div className="chat-input-toolbar-left" data-testid="chat-panel-input-toolbar-left">
          <input
            ref={fileInputRef}
            type="file"
            accept={ATTACHMENT_ACCEPT}
            multiple
            className="hidden"
            data-testid="chat-panel-input-file-input"
            onChange={handleFileInputChange}
          />
          <div ref={attachMenuRef} className="chat-input-attach-menu-anchor" data-testid="chat-panel-input-attach-menu-anchor">
            <button
              type="button"
              data-testid="chat-panel-input-attach-trigger"
              onClick={() => {
                if (attachTriggerDisabled) return;
                if (!attachMenuOpen && attachMenuRef.current) {
                  const rect = attachMenuRef.current.getBoundingClientRect();
                  setAttachMenuAnchor(rect);
                  setAttachMenuDirection(window.innerHeight - rect.bottom >= 200 ? 'down' : 'up');
                }
               setAttachMenuOpen((open) => !open);
               setExtensionPanelOpen(false);
                setSkillPanelOpen(false);
             }}
             disabled={attachTriggerDisabled}
              className={cx(
                'chat-input-btn chat-input-btn--add-file',
                attachTriggerDisabled && 'chat-input-btn--disabled',
                attachMenuOpen && 'chat-input-btn--menu-open',
              )}
              title={attachTriggerDisabled ? t('chat.addFileDisabled') : undefined}
              aria-label={attachTriggerDisabled ? t('chat.addFileDisabled') : t('chat.addFile')}
              aria-haspopup="menu"
              aria-expanded={attachMenuOpen}
              data-tooltip={attachTriggerDisabled ? t('chat.addFileDisabled') : t('chat.addFileTooltip')}
              {...attachTooltipHandlers}
            >
              <Plus className="chat-input-btn-icon" strokeWidth={1.8} />
            </button>
            {attachTooltipNode}
            {attachMenuOpen && attachMenuAnchor && createPortal(
              <div
                ref={attachMenuPortalRef}
                className="chat-mode-select__menu chat-input-attach-menu"
                role="menu"
                data-testid="chat-panel-input-attach-menu"
                style={attachMenuDirection === 'up'
                  ? { position: 'fixed', bottom: window.innerHeight - attachMenuAnchor.top + 10, left: attachMenuAnchor.left, zIndex: 9999 }
                  : { position: 'fixed', top: attachMenuAnchor.bottom + 10, left: attachMenuAnchor.left, zIndex: 9999 }
                }
              >
                <button
                  type="button"
                  className="chat-mode-select__option"
                  role="menuitem"
                  data-testid="chat-panel-input-attach-menu-file"
                  disabled={imageInputDisabled}
                  title={imageInputDisabled ? t('chat.addFileDisabled') : undefined}
                  onClick={() => {
                    void openAttachmentPicker();
                  }}
                >
                  <span className="chat-mode-select__option-main">
                    <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden="true">
                      <AttachmentIcon aria-hidden="true" />
                    </span>
                    <span className="chat-mode-select__label">{t('chat.addFile')}</span>
                  </span>
                </button>
                {!isTeamMode && <>
                <div className="chat-attach-menu-item-anchor">
                <button
                  type="button"
                  className={clsx(
                    'chat-mode-select__option chat-agent-picker-trigger',
                    agentPickerOpen && 'chat-mode-select__option--panel-open',
                  )}
                  role="menuitem"
                  aria-haspopup="menu"
                  aria-expanded={agentPickerOpen}
                  data-testid="chat-panel-input-attach-menu-agent"
                  onClick={() => {
                    setAgentPickerOpen((open) => !open);
                    setExtensionPanelOpen(false);
                    setSkillPanelOpen(false);
                  }}
                >
                    <span className="chat-mode-select__option-main">
                      <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden="true">
                      <AgentPickerIcon aria-hidden="true" />
                      </span>
                    <span className="chat-mode-select__label">{t('chat.agent')}</span>
                  </span>
                  <ChevronRight className="chat-agent-picker-trigger__chevron" size={15} aria-hidden="true" />
                </button>
                {agentPickerOpen && (
                  <PickerPanel
                    className="chat-agent-picker"
                    direction={attachMenuDirection}
                    ariaLabel={t('chat.agent')}
                    testId="chat-panel-agent-picker-panel"
                    onMouseEnter={() => setAgentPickerOpen(true)}
                    rowHeight={AGENT_PICKER_ROW_HEIGHT}
                    itemCount={filteredAgentOptions.length}
                    tabs={
                      <div className="chat-picker-panel__tabs" role="tablist" aria-label={t('agentManagement.detail.tabsLabel')}>
                        <span className="is-active" role="tab" aria-selected="true">{t('chat.agent')}</span>
                      </div>
                    }
                    search={
                      <label className="chat-picker-panel__search">
                        <div className="chat-picker-panel__search-inner">
                          <SearchIcon aria-hidden="true" />
                          <input
                            type="search"
                            value={agentPickerQuery}
                            onChange={(event) => setAgentPickerQuery(event.target.value)}
                            placeholder={t('chat.agentSearchPlaceholder')}
                            data-testid="chat-panel-agent-picker-search-input"
                          />
                        </div>
                      </label>
                    }
                    footer={{
                      label: t('chat.agentMore'),
                      onClick: () => {
                        setAttachMenuOpen(false);
                        onNavigateToAgents?.();
                      },
                    }}
                  >
                    {agentOptionsStatus === 'loading' ? (
                      <div className="chat-agent-picker__state" data-testid="chat-panel-agent-picker-state" data-variant="loading">{t('common.loading')}</div>
                    ) : agentOptionsStatus === 'error' ? (
                      <div className="chat-agent-picker__state" data-testid="chat-panel-agent-picker-state" data-variant="error">{t('agentManagement.states.loadError')}</div>
                    ) : filteredAgentOptions.length === 0 ? (
                      <div className="chat-agent-picker__state" data-testid="chat-panel-agent-picker-state" data-variant={installedAgentOptions.length === 0 ? 'no-installed' : 'no-matches'}>
                        {installedAgentOptions.length === 0 ? t('chat.agentNoInstalled') : t('chat.agentNoMatches')}
                      </div>
                    ) : (
                      filteredAgentOptions.map((item) => {
                        const avatarUrl = getAgentAvatarUrl(item);
                        const isSelected = selectedAgentId === item.id;
                        return (
                          <button
                            key={item.id}
                            type="button"
                            className={clsx('chat-agent-picker__item', isSelected && 'is-selected')}
                            role="menuitemradio"
                            aria-checked={isSelected}
                            data-testid="chat-panel-agent-picker-item"
                            data-variant={item.id}
                            data-tooltip={item.description || undefined}
                            {...agentTooltipHandlers}
                            onClick={() => {
                              if (!activeSessionId) return;
                              useSessionStore.getState().setMode(activeSessionId, 'agent');
                              setAgentSelectionIntent(activeSessionId, { kind: 'select', id: item.id });
                              setAttachMenuOpen(false);
                            }}
                          >
                            <span className="chat-agent-picker__avatar" aria-hidden="true">
                              {avatarUrl ? <img src={avatarUrl} alt="" /> : item.displayName.trim().slice(0, 1).toUpperCase() || '?'}
                            </span>
                            <span className="chat-agent-picker__item-name">{item.displayName}</span>
                            {isSelected && (
                              <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                              </svg>
                            )}
                          </button>
                        );
                      })
                    )}
                  </PickerPanel>
                )}
                {agentTooltipNode}
                </div>
                </>}
                {/* 插件/MCP 装备目前后端在集群模式下不生效（JiuWenSwarmDeepAdapter
                    ._ensure_chat_extensions 对 team 模式直接短路，见
                    interface_deep.py），继续展示这个入口只会让用户以为选了插件/MCP 会生效，
                    实际发出去也是白发。集群模式下直接不渲染这个入口，跟旁边 SkillSelector
                    （!isTeamMode 判断）保持同样的处理方式。 */}
                <div className="chat-attach-menu-item-anchor">
                <button
                  type="button"
                  className={clsx(
                    'chat-mode-select__option',
                    skillPanelOpen && 'chat-mode-select__option--panel-open',
                  )}
                  role="menuitem"
                  aria-haspopup="menu"
                  aria-expanded={skillPanelOpen}
                  data-testid="chat-panel-input-attach-menu-skill"
                  onClick={() => {
                    setSkillPanelOpen((open) => !open);
                    setAgentPickerOpen(false);
                    setExtensionPanelOpen(false);
                  }}
                >
                  <span className="chat-mode-select__option-main">
                    <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden="true">
                      <SkillIcon aria-hidden="true" />
                    </span>
                    <span className="chat-mode-select__label">{t(isTeamMode ? 'chat.swarmSkills' : 'chat.skills')}</span>
                  </span>
                  <ChevronRight className="chat-mode-select__chevron" size={16} aria-hidden="true" />
                </button>
                {skillPanelOpen && (
                  <SkillPickerPanel
                    panelRef={skillPanelRef}
                    direction={attachMenuDirection}
                    isTeamMode={isTeamMode}
                    onClose={() => setSkillPanelOpen(false)}
                    onNavigateToSkills={onNavigateToSkills}
                    onInsertSkill={insertSkillChip}
                    onRemoveSkill={removeSkillChip}
                  />
                )}
                </div>
                {!isTeamMode && (
                  <div className="chat-attach-menu-item-anchor">
                  <button
                    type="button"
                    className={clsx(
                      'chat-mode-select__option',
                      extensionPanelOpen && 'chat-mode-select__option--panel-open',
                    )}
                    role="menuitem"
                    aria-haspopup="menu"
                    aria-expanded={extensionPanelOpen}
                    data-testid="chat-panel-input-attach-menu-extension"
                    onClick={() => {
                      setExtensionPanelOpen((open) => !open);
                      setAgentPickerOpen(false);
                      setSkillPanelOpen(false);
                    }}
                  >
                    <span className="chat-mode-select__option-main">
                      <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden="true">
                        <ExtensionIcon />
                      </span>
                      <span className="chat-mode-select__label">{t('chat.extension')}</span>
                    </span>
                    <ChevronRight className="chat-mode-select__chevron" size={16} aria-hidden="true" />
                  </button>
                  {extensionPanelOpen && (
                    <ExtensionPickerPanel
                      panelRef={extensionPanelRef}
                      direction={attachMenuDirection}
                      onClose={() => setExtensionPanelOpen(false)}
                    />
                  )}
                  </div>
                )}
                <div className="chat-mode-select__divider" role="separator" />
                {canUsePlanMenu && (() => {
                  // 能否切换计划模式由 planModeGate 统一判断，与 `/plan` 命令、
                  // 「计划」chip 关闭按钮共用同一套限制：打开方向受「有未完成目标 /
                  // 会话忙」限制，关闭方向受「会话忙」限制。「会话忙」= 对话进行中 /
                  // 暂停中 / 等待 ask_user 回答（后者 isProcessing 已回到 false，
                  // 但会话没结束，必须一并算忙）。
                  const planToggleDecision = evaluatePlanToggle(activeSessionId, !planActive);
                  const planDisabled = !planToggleDecision.ok;
                  const planTitle = planToggleDecision.reason ? t(planToggleDecision.reason) : undefined;
                  const togglePlan = (next: boolean) => {
                    if (!activeSessionId) return;
                    // 命中限制直接返回（UI 靠 disabled 视觉，无需额外提示条）；
                    // 通过后再跑「打开方向」的互斥副作用，最后由 applyPlanToggle 落状态。
                    if (!evaluatePlanToggle(activeSessionId, next).ok) return;
                    if (next) {
                      // Plan 与 Swarmflow 互斥：开启 Plan 前先关掉 Swarmflow。
                      if (useSessionStore.getState().getRuntime(activeSessionId)?.enableSwarmflow) {
                        useSessionStore.getState().setSwarmflowActive(activeSessionId, false);
                        setSwarmflowConfigPanelOpen(false);
                      }
                      // goalArmed 为 true 时只可能是"刚选了目标、还没发消息"的
                      // 未提交态，顶掉换成 Plan。
                      useGoalStore.getState().setArmed(activeSessionId, false);
                    }
                    applyPlanToggle(activeSessionId, next, { entrySource: 'plan_toggle' });
                    // 不关闭菜单：用户拨动开关后保持菜单打开，便于看到开关状态变化并继续操作。
                  };
                  return (
                    <div
                      className={cx('chat-mode-select__option', planDisabled && 'chat-mode-select__option--disabled')}
                      role="menuitem"
                      data-testid="chat-panel-input-attach-menu-plan"
                      title={planTitle}
                      onClick={() => {
                        if (planDisabled) return;
                        togglePlan(!planActive);
                      }}
                    >
                      <span className="chat-mode-select__option-main">
                        <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden="true">
                          <PlanIcon aria-hidden="true" />
                        </span>
                        <span className="chat-mode-select__label">{t('plan.toggleLabel')}</span>
                      </span>
                      <Switch checked={planActive} disabled={planDisabled} onChange={togglePlan} />
                    </div>
                  );
                })()}
                {isTeamMode && (() => {
                  const toggleSwarmflow = (next: boolean) => {
                    if (!activeSessionId || swarmflowToggleDisabled) return;
                    // Swarmflow 与 Plan 互斥：开启 Swarmflow 前先关掉 Plan。
                    // 这是内部互斥复位（只在 Plan 未提交且会话空闲时可达），不是
                    // 用户主动退出计划模式，故直接 setActive、不过 planModeGate。
                    if (next && planActive) {
                      usePlanStore.getState().setActive(activeSessionId, false);
                    }
                    useSessionStore.getState().setSwarmflowActive(activeSessionId, next);
                  };
                  return (
                    <div
                      className={cx('chat-mode-select__option', swarmflowToggleDisabled && 'chat-mode-select__option--disabled')}
                      role="menuitem"
                      data-testid="chat-panel-input-attach-menu-swarmflow"
                    >
                      <span
                        className="chat-mode-select__option-main"
                        onClick={() => {
                          if (swarmflowToggleDisabled) return;
                          toggleSwarmflow(!swarmflowActive);
                        }}
                      >
                        <span className="chat-mode-select__icon" aria-hidden="true">
                          <Workflow className="w-4 h-4" />
                        </span>
                        <span className="chat-mode-select__label">{t('swarmflow.toggleLabel')}</span>
                      </span>
                      <div className="chat-mode-select__option-actions">
                        <button
                          ref={swarmflowConfigBtnRef}
                          type="button"
                          className={cx('chat-mode-select__config-btn', !swarmflowActive && 'chat-mode-select__config-btn--disabled')}
                          aria-haspopup="menu"
                          aria-expanded={swarmflowConfigPanelOpen}
                          data-testid="chat-panel-input-attach-menu-swarmflow-config"
                          title={t('swarmflow.configTitle')}
                          disabled={!swarmflowActive}
                          onClick={(e) => {
                            e.stopPropagation();
                            if (!swarmflowActive) return;
                            if (!swarmflowConfigPanelOpen && swarmflowConfigBtnRef.current) {
                              setSwarmflowConfigAnchor(swarmflowConfigBtnRef.current.getBoundingClientRect());
                            }
                            setSwarmflowConfigPanelOpen((open) => !open);
                          }}
                        >
                          <Settings className="w-3.5 h-3.5" aria-hidden="true" />
                        </button>
                        <Switch checked={swarmflowActive} disabled={swarmflowToggleDisabled} onChange={toggleSwarmflow} />
                      </div>
                    </div>
                  );
                })()}
                {canUseGoalMenu && (() => {
                  // Goal 和 Plan 互斥：已有真正生效的计划时不能再选目标；"打开"方向沿用原逻辑，
                  // "关闭"方向不受限制（跟输入框旁边现有的目标 chip 关闭按钮一致，随时可关）。
                  const goalChecked = goalArmed || hasUnfinishedGoal;
                  const goalDisabledOn = hasUnfinishedGoal || planCommitted;
                  const goalDisabledOnTitle = hasUnfinishedGoal
                    ? t('goal.toolbarUnavailable')
                    : planCommitted
                      ? t('goal.toolbarUnavailablePlan')
                      : undefined;
                  const goalDisabled = goalChecked ? false : goalDisabledOn;
                  const goalTitle = goalChecked ? undefined : goalDisabledOnTitle;
                  const toggleGoal = (next: boolean) => {
                    if (!activeSessionId) return;
                    if (next) {
                      if (goalDisabledOn) return;
                      // 走到这里 planCommitted 一定是 false（否则上面已 disabled），所以 planActive
                      // 为 true 时只可能是"刚打开开关、还没发过消息"的未提交态，可以放心顶掉。
                      // 内部互斥复位（非用户主动退出计划模式），直接 setActive、不过 planModeGate。
                      if (planActive) {
                        usePlanStore.getState().setActive(activeSessionId, false);
                      }
                      useGoalStore.getState().setArmed(activeSessionId, true);
                    } else {
                      if (currentGoal) {
                        onClearGoal?.(activeSessionId);
                      }
                      useGoalStore.getState().setArmed(activeSessionId, false);
                    }
                    // 不关闭菜单：用户拨动开关后保持菜单打开，便于看到开关状态变化并继续操作。
                  };
                  return (
                    <div
                      className={cx('chat-mode-select__option', goalDisabled && 'chat-mode-select__option--disabled')}
                      role="menuitem"
                      data-testid="chat-panel-input-attach-menu-goal"
                      title={goalTitle}
                      onClick={() => {
                        if (goalDisabled) return;
                        toggleGoal(!goalChecked);
                      }}
                    >
                      <span className="chat-mode-select__option-main">
                        <span className="chat-mode-select__icon chat-mode-select__icon--asset" aria-hidden="true">
                          <GoalIcon aria-hidden="true" />
                        </span>
                        <span className="chat-mode-select__label">{t('goal.toggleLabel')}</span>
                      </span>
                      <Switch checked={goalChecked} disabled={goalDisabled} onChange={toggleGoal} />
                    </div>
                  );
                })()}
              </div>,
              document.body
             )}
          </div>
          <div
            ref={modeMenuRef}
            className={clsx(
              'chat-mode-select',
              isModeMenuOpen && 'chat-mode-select--open',
            )}
            data-testid="chat-panel-mode-select"
          >
            <button
              type="button"
              className="chat-mode-select__trigger"
              data-testid="chat-panel-mode-select-trigger"
              data-variant={currentMode.value}
              onClick={() => {
                if (hasHistory || isProcessing) return;
                if (!isModeMenuOpen && modeMenuRef.current) {
                  const rect = modeMenuRef.current.getBoundingClientRect();
                  setMenuDirection(resolveMenuDirection(rect.bottom, 160));
                  setModeMenuAnchor(rect);
                }
                setIsModeMenuOpen((open) => !open);
              }}
              aria-haspopup="menu"
              aria-expanded={isModeMenuOpen}
              style={(hasHistory || isProcessing) ? { cursor: 'default' } : undefined}
            >
              <span className="chat-mode-select__value" data-testid="chat-panel-mode-select-value">
                <span className="chat-mode-select__icon" aria-hidden="true">
                  <currentMode.icon className="w-4 h-4" />
                </span>
                <span className="chat-mode-select__label">{t(currentMode.i18nKey)}</span>
              </span>
              {!hasHistory && !isProcessing && (
                <svg className="chat-mode-select__chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
                </svg>
              )}
            </button>

            {isModeMenuOpen && modeMenuAnchor && createPortal(
              <div
                ref={modeMenuPortalRef}
                className="chat-mode-select__menu chat-mode-select__menu--agent-modes"
                role="menu"
                data-testid="chat-panel-mode-select-menu"
                style={menuDirection === 'up'
                  ? { position: 'fixed', bottom: window.innerHeight - modeMenuAnchor.top + 10, left: modeMenuAnchor.left, zIndex: 9999 }
                  : { position: 'fixed', top: modeMenuAnchor.bottom + 10, left: modeMenuAnchor.left, zIndex: 9999 }
                }
              >
                {AGENT_MODE_OPTIONS.map((m) => (
                  <button
                    type="button"
                    key={m.value}
                    onClick={() => void handleModeSelect(m.value)}
                    onMouseEnter={(e) => {
                      const desc = m.descriptionI18nKey ? t(m.descriptionI18nKey) : null;
                      setHoveredOptionDesc(desc);
                      setHoveredOptionRect(desc ? e.currentTarget.getBoundingClientRect() : null);
                    }}
                    onMouseLeave={() => {
                      setHoveredOptionDesc(null);
                      setHoveredOptionRect(null);
                    }}
                    className={clsx(
                      'chat-mode-select__option',
                      mode === m.value && 'chat-mode-select__option--active',
                    )}
                    role="menuitemradio"
                    aria-checked={mode === m.value}
                    data-testid="chat-panel-mode-select-option"
                    data-variant={m.value}
                  >
                    <span className="chat-mode-select__option-main">
                      <span className="chat-mode-select__icon" aria-hidden="true">
                        <m.icon className="w-4 h-4" />
                      </span>
                      <span className="chat-mode-select__label">{t(m.i18nKey)}</span>
                    </span>
                    {mode === m.value && (
                      <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                      </svg>
                    )}
                  </button>
                  ))}
                </div>,
                document.body
              )}
              {isModeMenuOpen && hoveredOptionDesc && hoveredOptionRect && createPortal(
                <div
                  className="adaptive-tooltip"
                  data-testid="chat-panel-mode-select-tooltip"
                  style={{
                    position: 'fixed',
                    top: hoveredOptionRect.top + (hoveredOptionRect.height / 2) - 17,
                    left: hoveredOptionRect.right + 11,
                    zIndex: 10000,
                  }}
                >
                  {hoveredOptionDesc}
                </div>,
                document.body
              )}
            </div>
          <PermissionSelector permissionsEnabled={permissionsEnabled} onSavePermission={onSavePermission} />

          {selectedAgentId && (
            <div className="chat-agent-tag">
              <span className="chat-agent-tag__avatar" aria-hidden="true">
                {selectedAgent && getAgentAvatarUrl(selectedAgent) ? (
                  <img src={getAgentAvatarUrl(selectedAgent) || ''} alt="" />
                ) : (
                  (selectedAgent?.displayName || selectedAgentId).trim().slice(0, 1).toUpperCase() || '?'
                )}
              </span>
              <span className="chat-agent-tag__label">{selectedAgent?.displayName || selectedAgentId}</span>
              <button
                type="button"
                className="chat-agent-tag__close"
                title={t('chat.agentRemove')}
                aria-label={t('chat.agentRemove')}
                onClick={() => {
                  if (activeSessionId) setAgentSelectionIntent(activeSessionId, { kind: 'clear' });
                }}
              >
                <X size={16} strokeWidth={2.5} aria-hidden="true" />
              </button>
            </div>
          )}
          {goalTagVisible && (
            <div className="chat-agent-tag" data-testid="chat-panel-goal-tag">
              <span className="chat-agent-tag__avatar chat-agent-tag__avatar--plain" aria-hidden="true">
                <GoalIcon aria-hidden="true" />
              </span>
              <span className="chat-agent-tag__label" data-testid="chat-panel-goal-tag-label">{t('goal.toolbarTag')}</span>
              <button
                type="button"
                className="chat-agent-tag__close"
                data-testid="chat-panel-goal-tag-close"
                title={t('goal.closeTag')}
                aria-label={t('goal.closeTag')}
                onClick={() => {
                  if (!activeSessionId) return;
                  if (currentGoal) {
                    onClearGoal?.(activeSessionId);
                  }
                  useGoalStore.getState().setArmed(activeSessionId, false);
                }}
              >
                <X size={16} strokeWidth={2.5} aria-hidden="true" />
              </button>
            </div>
          )}

          {planTagVisible && (
            <div className="chat-agent-tag" data-testid="chat-panel-plan-tag">
              <span className="chat-agent-tag__avatar chat-agent-tag__avatar--plain" aria-hidden="true">
                <PlanIcon aria-hidden="true" />
              </span>
              <span className="chat-agent-tag__label" data-testid="chat-panel-plan-tag-label">{t('plan.toolbarTag')}</span>
              {(() => {
                // 关闭「计划」chip 与输入框旁 Plan 开关、`/plan` 命令共用同一套限制
                // （planModeGate）：会话进行中 / 暂停 / 等待 ask_user 回答时不可退出。
                const closeBlocked = !evaluatePlanToggle(activeSessionId, false).ok;
                return (
                  <button
                    type="button"
                    className="chat-agent-tag__close"
                    data-testid="chat-panel-plan-tag-close"
                    disabled={closeBlocked}
                    title={closeBlocked ? t('plan.closeTagDisabled') : t('plan.closeTag')}
                    aria-label={closeBlocked ? t('plan.closeTagDisabled') : t('plan.closeTag')}
                    onClick={() => {
                      applyPlanToggle(activeSessionId, false);
                    }}
                  >
                    <X size={16} strokeWidth={2.5} aria-hidden="true" />
                  </button>
                );
              })()}
            </div>
          )}

          {swarmflowActive && (
            <div className="chat-goal-tag" data-testid="chat-panel-swarmflow-tag">
              <button
                type="button"
                className="chat-mode-select__trigger"
                data-testid="chat-panel-swarmflow-tag-label"
                title={t('swarmflow.tagHint')}
              >
                <span className="chat-mode-select__value">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <Workflow className="w-3.5 h-3.5" />
                  </span>
                  <span className="chat-mode-select__label">{t('swarmflow.toggleLabel')}</span>
                </span>
              </button>
              <button
                type="button"
                className="chat-goal-tag__close"
                data-testid="chat-panel-swarmflow-tag-close"
                disabled={swarmflowToggleDisabled}
                title={swarmflowToggleDisabled ? t('swarmflow.closeTagDisabled') : t('swarmflow.closeTag')}
                onClick={() => {
                  if (swarmflowToggleDisabled) return;
                  if (!activeSessionId) return;
                  useSessionStore.getState().setSwarmflowActive(activeSessionId, false);
                  setSwarmflowConfigPanelOpen(false);
                }}
              >
                <X size={11} strokeWidth={2.5} />
              </button>
            </div>
          )}

          {evolutionLabel && (
            <div className="chat-input-evolution-pill" data-testid="chat-panel-input-evolution-pill" title={evolutionLabel}>
              <span className="chat-input-evolution-pill__dot" />
              <span className="chat-input-evolution-pill__label">{evolutionLabel}</span>
            </div>
          )}
        </div>

        <div className="chat-input-actions" data-testid="chat-panel-input-actions">
          {/* {speechSupported && (
            <button
              type="button"
              onPointerDown={handleVoicePointerDown}
              onPointerUp={handleVoicePointerUp}
              onPointerCancel={handleVoicePointerCancel}
              className={cx(
                'chat-input-btn',
                isListening && 'chat-input-btn--recording',
              )}
              title={t('chat.holdToSpeak')}
            >
              {isListening ? (
                <svg className="chat-input-btn-icon" fill="currentColor" viewBox="0 0 24 24">
                  <rect x="6" y="6" width="12" height="12" rx="2" />
                </svg>
              ) : (
                <svg className="chat-input-btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.8}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 01-3-3V4.5a3 3 0 116 0v8.25a3 3 0 01-3 3z" />
                </svg>
              )}
            </button>
          )} */}

          {(isAgentMode || isTeamMode) && <ContextUsageIndicator />}

          <ChatModelSelector
            disabled={isProcessing || composerDisabled || (!isAgentMode && activeSessionId !== NEW_CONVERSATION_ID)}
          />

          <button
            type="button"
            onClick={handleSendButtonClick}
            disabled={!canSubmit}
            className={cx(
              'chat-input-btn chat-input-btn--send',
              showStop && 'chat-input-btn--stop',
              canSubmit ? 'chat-input-btn--send-active' : 'chat-input-btn--disabled',
            )}
            title={showStop ? t('chat.stop') : t('chat.send')}
            data-testid="chat-panel-input-send"
            data-variant={showStop ? 'stop' : 'send'}
          >
            {showStop ? (
              <Square className="chat-input-btn-icon" fill="currentColor" strokeWidth={1.8} aria-hidden="true" />
            ) : (
              <img
                className="chat-input-btn-icon chat-input-btn-icon--image"
                src={canSubmit ? sendActiveIcon : sendIcon}
                alt=""
                aria-hidden="true"
              />
            )}
          </button>
        </div>
      </div>
      </div>

      {showWorkContextRow ? (
        <div className="chat-work-context-wrapper" data-testid="chat-panel-work-context-wrapper">
          {showSlashSuggestionBelow && composerSuggestion && (
            <ComposerSuggestionMenu
              suggestion={composerSuggestion}
              items={composerSuggestionItems}
              highlightedIndex={composerSuggestionIndex}
              navigationMode={composerSuggestionNavigationMode}
              containerRef={composerSuggestionMenuRef}
              onPointerHighlight={(index) => {
                setComposerSuggestionNavigationMode('pointer');
                setComposerSuggestionIndex(index);
              }}
              onPick={insertComposerToken}
              loading={slashCatalogLoading}
              slashSkillsOnly={isTeamMode}
              placement="below"
            />
          )}
        <div ref={workMenuRef} className="chat-work-context-row" data-testid="chat-panel-work-context-row">
          <div className={clsx('chat-work-select', workMenuOpen === 'project' && 'chat-work-select--open')} data-testid="chat-panel-work-select">
            <button
              type="button"
              className={clsx('chat-work-select__trigger', displayedProject && 'chat-work-select__trigger--selected')}
              data-testid="chat-panel-work-select-trigger"
              onClick={() => !isWorkContextLocked && setWorkMenuOpen((open) => open === 'project' ? null : 'project')}
              disabled={isWorkContextLocked}
              title={displayedProject?.project_dir || (isWorkContextLocked ? t('multiSession.project.lockedProjectTitle') : t('multiSession.project.chooseProjectDirectory'))}
            >
              <WorkIcon name="folder" className="chat-work-select__root-icon" />
              <span>{getProjectLabel(displayedProject, t('multiSession.project.chooseProjectDirectory'))}</span>
              {displayedProject && !isWorkContextLocked ? (
                <span className="chat-work-select__trigger-action">
                  <svg className="chat-work-select__chevron" width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
                  </svg>
                  <button
                    type="button"
                    className="chat-work-select__clear"
                    data-testid="chat-panel-work-select-clear"
                    aria-label={t('multiSession.project.clearProject')}
                    data-tooltip={t('multiSession.project.clearProject')}
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectedProject(null);
                      setWorkMenuOpen(null);
                    }}
                  >
                    <WorkIcon name="close" />
                  </button>
                </span>
              ) : (
                <svg className="chat-work-select__chevron" width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
                </svg>
              )}
            </button>
            {workMenuOpen === 'project' && !isWorkContextLocked ? (
              <div className={clsx('chat-work-select__menu', hasInputProjectOptions && 'chat-work-select__menu--projects')} role="menu" data-testid="chat-panel-work-select-menu">
                {!hasInputProjectOptions ? (
                  <ProjectCreateMenu
                    onCreate={(mode) => {
                      void openProjectCreateDialog(mode);
                    }}
                    itemClassName="chat-work-select__option chat-work-select__option--compact"
                    blankIcon={<WorkIcon name="add" />}
                    existingIcon={<WorkIcon name="folder" />}
                  />
                ) : (
                  <>
                    <label className="chat-work-select__search-wrap">
                      <WorkIcon name="search" />
                      <input
                        className="chat-work-select__search"
                        data-testid="chat-panel-work-select-search"
                        value={projectSearch}
                        onChange={(event) => setProjectSearch(event.target.value)}
                        placeholder={t('multiSession.project.searchProject')}
                      />
                    </label>
                    <div className="chat-work-select__options" data-testid="chat-panel-work-select-options">
                      {inputProjectOptions.map((project) => {
                        const active = selectedProject?.project_id === project.project_id;
                        return (
                          <button
                            type="button"
                            key={project.project_id}
                            className={clsx('chat-work-select__option', active && 'is-active')}
                            data-testid="chat-panel-work-select-option"
                            data-variant={project.project_id}
                            onClick={() => {
                              setSelectedProject(project);
                              setWorkMenuOpen(null);
                            }}
                            role="menuitemradio"
                            aria-checked={active}
                            title={project.project_dir}
                          >
                            <WorkIcon name="folder" />
                            <span>{project.name}</span>
                            {active ? <WorkIcon name="check" className="chat-work-select__check" /> : null}
                          </button>
                        );
                      })}
                      {inputProjectOptions.length === 0 ? (
                        <div className="chat-work-select__empty" data-testid="chat-panel-work-select-empty">{t('multiSession.project.noProjectMatches')}</div>
                      ) : null}
                    </div>
                    <ProjectAddSubmenu
                      onCreate={(mode) => {
                        void openProjectCreateDialog(mode);
                      }}
                    />
                  </>
                )}
              </div>
            ) : null}
          </div>
          {workMode === 'code' ? (
            <CodeBranchSelector project={displayedProject} disabled={isProcessing} compact />
          ) : null}
          {projectDirError && !workDialogOpen ? (
            <div className="app-toast-wrapper app-toast-wrapper--top-center">
              <div className="app-session-toast" role="status" aria-live="polite" data-testid="chat-panel-work-select-error-toast">
                {projectDirError}
              </div>
            </div>
          ) : null}
        </div>
        </div>
      ) : null}

      {workDialogOpen ? (
        <div className="chat-work-dialog-backdrop" role="presentation" data-testid="chat-panel-work-dialog">
          <form
            className="chat-work-dialog"
            data-testid="chat-panel-work-dialog-form"
            onSubmit={(event) => {
              event.preventDefault();
              void handleAddProjectDir();
            }}
          >
            <button
              type="button"
              className="chat-work-dialog__close"
              data-testid="chat-panel-work-dialog-close"
              aria-label={t('common.close')}
              onClick={() => {
                setProjectDirDraft('');
                setProjectNameDraft('');
                setProjectDirError(null);
                setWorkDialogOpen(false);
              }}
            >
              <WorkIcon name="close" />
            </button>
            <div className="chat-work-dialog__title" data-testid="chat-panel-work-dialog-title">
              {projectCreateMode === 'existing'
                ? t('multiSession.project.selectExisting')
                : t('multiSession.project.createBlank')}
            </div>
            <input
              className="chat-work-dialog__input"
              data-testid="chat-panel-work-dialog-name-input"
              value={projectNameDraft}
              onChange={(event) => setProjectNameDraft(event.target.value)}
              placeholder={t('multiSession.project.namePlaceholder')}
              autoFocus
            />
            {projectCreateMode === 'existing' ? (
              <input
                className="chat-work-dialog__input"
                data-testid="chat-panel-work-dialog-path-input"
                data-variant="existing"
                value={projectDirDraft}
                onChange={(event) => setProjectDirDraft(event.target.value)}
                placeholder={t('multiSession.project.pathPlaceholder')}
              />
            ) : null}
            {projectDirError ? <div className="chat-work-dialog__error" data-testid="chat-panel-work-dialog-error">{projectDirError}</div> : null}
            <div className="chat-work-dialog__actions" data-testid="chat-panel-work-dialog-actions">
              <button
                type="button"
                data-testid="chat-panel-work-dialog-cancel"
                onClick={() => {
                  setProjectDirDraft('');
                  setProjectNameDraft('');
                  setProjectDirError(null);
                  setWorkDialogOpen(false);
                }}
              >
                {t('multiSession.project.cancel')}
              </button>
              <button
                type="submit"
                data-testid="chat-panel-work-dialog-confirm"
                disabled={!projectNameDraft.trim() || (projectCreateMode === 'existing' && !projectDirDraft.trim())}
              >
                {t('multiSession.project.confirm')}
              </button>
            </div>
          </form>
        </div>
      ) : null}
      {swarmflowConfigPanelOpen && swarmflowConfigAnchor && activeSessionId && swarmflowActive && (() => {
        // 方向自适应：齿轮下方空间不足时向上展开，避免卡片被视口底部截断
        const panelHeight = 180;
        const spaceBelow = window.innerHeight - swarmflowConfigAnchor.bottom;
        const openUpward = spaceBelow < panelHeight + 16;
        const panelStyle: CSSProperties = {
          position: 'fixed',
          ...(openUpward
            ? { bottom: window.innerHeight - swarmflowConfigAnchor.top + 8 }
            : { top: swarmflowConfigAnchor.top }),
          left: swarmflowConfigAnchor.right + 8,
          zIndex: 9999,
        };
        return createPortal(
        <div
          ref={swarmflowConfigPanelRef}
          className="chat-swarmflow-config-panel"
          data-testid="chat-panel-swarmflow-config-panel"
          style={panelStyle}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="chat-swarmflow-config-panel__header">
            <span className="chat-swarmflow-config-panel__title">{t('swarmflow.configTitle')}</span>
            <button
              type="button"
              onClick={() => setSwarmflowConfigPanelOpen(false)}
              className="chat-swarmflow-config-panel__close"
              aria-label={t('common.close')}
              data-testid="chat-panel-swarmflow-config-close"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
          <div className="chat-swarmflow-config-panel__field">
            <label className="chat-swarmflow-config-panel__label">{t('swarmflow.budgetPanelHint')}</label>
              {(() => {
                // 从 store 的 swarmflowBudget（实际 token 数）反推输入框值 + 单位
                const budgetVal = swarmflowBudget;
                const isUnlimited = budgetVal == null;
                let inputValue = '';
                let inputUnit: 'token' | 'K' | 'M' = 'K';
                if (!isUnlimited && budgetVal > 0) {
                  if (budgetVal >= 1_000_000 && budgetVal % 1_000_000 === 0) {
                    inputValue = String(budgetVal / 1_000_000);
                    inputUnit = 'M';
                  } else if (budgetVal >= 1000 && budgetVal % 1000 === 0) {
                    inputValue = String(budgetVal / 1000);
                    inputUnit = 'K';
                  } else {
                    inputValue = String(budgetVal);
                    inputUnit = 'token';
                  }
                }
                const computeBudget = (val: string, unit: 'token' | 'K' | 'M') => {
                  if (!val.trim()) return null;
                  const n = Number(val.trim());
                  // token 是最小计费单位,后端 int() 截断浮点会丢精度,
                  // 故前端只接受正整数;浮点 / 非数字 / ≤0 一律视作无效。
                  if (!Number.isInteger(n) || n <= 0) return null;
                  const multiplier = unit === 'M' ? 1_000_000 : unit === 'K' ? 1000 : 1;
                  return n * multiplier;
                };
                return (
                  <>
                    <div className="chat-swarmflow-config-panel__budget-row">
                      <Input
                        type="number"
                        step={1}
                        min={1}
                        value={inputValue}
                        placeholder={isUnlimited ? '' : t('swarmflow.budgetPlaceholder')}
                        readOnly={swarmflowToggleDisabled}
                        onChange={(v) => {
                          if (swarmflowToggleDisabled) return;
                          const unit = inputUnit;
                          const actual = computeBudget(v, unit);
                          // 输入有效数字→设置上限（自动取消"无限制"）；
                          // 输入空/非数字→回退无限制。
                          useSessionStore.getState().setSwarmflowActive(
                            activeSessionId, true, actual,
                          );
                        }}
                      />
                      <Select
                        value={inputUnit}
                        disabled={swarmflowToggleDisabled}
                        options={[
                          { value: 'token', label: 'token' },
                          { value: 'K', label: 'K (×1,000)' },
                          { value: 'M', label: 'M (×1,000,000)' },
                        ]}
                        onChange={(val) => {
                          if (swarmflowToggleDisabled) return;
                          const unit = val as 'token' | 'K' | 'M';
                          const actual = computeBudget(inputValue || '500', unit);
                          useSessionStore.getState().setSwarmflowActive(
                            activeSessionId, true, actual,
                          );
                        }}
                      />
                    </div>
                    <label className="chat-swarmflow-config-panel__unlimited-row">
                      <input
                        type="checkbox"
                        checked={isUnlimited}
                        disabled={swarmflowToggleDisabled}
                        onChange={(e) => {
                          if (swarmflowToggleDisabled) return;
                          if (e.target.checked) {
                            // 勾选"无限制"→ budget=null，数字自动清空（反推时 inputValue=''）
                            useSessionStore.getState().setSwarmflowActive(
                              activeSessionId, true, null,
                            );
                          } else {
                            // 取消勾选→给默认值 500K
                            useSessionStore.getState().setSwarmflowActive(
                              activeSessionId, true, 500000,
                            );
                          }
                        }}
                      />
                      <span className="text-xs text-text-muted">{t('swarmflow.budgetUnlimited')}</span>
                    </label>
                    {!isUnlimited && (() => {
                      const actual = computeBudget(inputValue, inputUnit);
                      return actual != null ? (
                        <div className="chat-swarmflow-config-panel__actual-hint">
                          {t('swarmflow.budgetActualHint', { count: actual.toLocaleString() })}
                        </div>
                      ) : null;
                    })()}
                  </>
                );
              })()}
              {swarmflowToggleDisabled && (
                <span className="chat-swarmflow-config-panel__readonly-hint">{t('swarmflow.configReadonlyHint')}</span>
              )}
            </div>
        </div>,
        document.body,
      );})()}
        </div>
      </div>
    </>
  );
});

function ProjectAddSubmenu({ onCreate }: { onCreate: (mode: ProjectCreateMode) => void }) {
  const { t } = useTranslation();
  return (
    <div className="chat-work-select__add" role="none" data-testid="chat-panel-work-select-add">
      <button
        type="button"
        className="chat-work-select__option chat-work-select__option--compact"
        role="menuitem"
        data-testid="chat-panel-work-select-add-trigger"
        aria-haspopup="menu"
      >
        <WorkIcon name="add" />
        <span>{t('multiSession.project.addNewProject')}</span>
        <WorkIcon name="arrow" className="chat-work-select__arrow" />
      </button>
      <div className="chat-work-select__submenu" role="menu">
        <ProjectCreateMenu
          onCreate={onCreate}
          itemClassName="chat-work-select__option chat-work-select__option--compact"
          blankIcon={<WorkIcon name="add" />}
          existingIcon={<WorkIcon name="folder" />}
        />
      </div>
    </div>
  );
}

function ComposerSuggestionMenu({
  suggestion,
  items,
  highlightedIndex,
  navigationMode,
  containerRef,
  onPointerHighlight,
  onPick,
  loading,
  slashSkillsOnly,
  placement = 'above',
}: {
  suggestion: ComposerSuggestionState;
  items: ComposerSuggestionItem[];
  highlightedIndex: number;
  navigationMode: 'keyboard' | 'pointer';
  containerRef?: RefObject<HTMLDivElement>;
  onPointerHighlight: (index: number) => void;
  onPick: (
    kind: ComposerSuggestionKind,
    value: string,
    label: string,
    slashItemKind?: 'command' | 'skill',
    slashTakesArgs?: boolean,
  ) => void;
  loading: boolean;
  slashSkillsOnly: boolean;
  placement?: 'above' | 'below';
}) {
  const isSlash = suggestion.kind === 'slash';
  const tokenPrefix = suggestion.kind === 'role' ? '$' : '@';
  const { t } = useTranslation();
  const commandCount = items.filter((item) => item.itemKind === 'command').length;
  const skillCount = items.filter((item) => item.itemKind === 'skill').length;
  const listRef = useRef<HTMLDivElement>(null);
  const activeItemRef = useRef<HTMLButtonElement>(null);
  const [slashListMaxHeight, setSlashListMaxHeight] = useState<number>();

  useEffect(() => {
    if (!isSlash || placement === 'below') {
      setSlashListMaxHeight(undefined);
      return;
    }
    const updateMaxHeight = () => {
      const list = listRef.current;
      const frameTop = list?.closest('.chat-input-container')?.getBoundingClientRect().top;
      if (frameTop == null) return;
      const headerBottom = list
        ?.closest('.chat-panel-shell')
        ?.querySelector<HTMLElement>('.chat-panel-header')
        ?.getBoundingClientRect().bottom ?? 16;
      // Existing conversations open the picker above the composer. Cap it to
      // the actual free space so a long skill list cannot cover the header.
      setSlashListMaxHeight(Math.max(
        0,
        Math.min(320, Math.floor(frameTop - headerBottom - 16)),
      ));
    };
    updateMaxHeight();
    window.addEventListener('resize', updateMaxHeight);
    return () => window.removeEventListener('resize', updateMaxHeight);
  }, [isSlash, placement]);

  useEffect(() => {
    const list = listRef.current;
    const activeItem = activeItemRef.current;
    if (!list || !activeItem) return;
    if (highlightedIndex === 0) {
      list.scrollTop = 0;
      return;
    }
    const listRect = list.getBoundingClientRect();
    const itemRect = activeItem.getBoundingClientRect();
    if (itemRect.top < listRect.top) {
      list.scrollTop -= listRect.top - itemRect.top;
    } else if (itemRect.bottom > listRect.bottom) {
      list.scrollTop += itemRect.bottom - listRect.bottom;
    }
  }, [highlightedIndex, items.length]);

  return (
    <div
      ref={containerRef}
      className={clsx(
        'chat-composer-suggestion',
        isSlash && 'chat-composer-suggestion--slash',
        isSlash && placement === 'below' && 'chat-composer-suggestion--below',
        navigationMode === 'keyboard' && 'chat-composer-suggestion--keyboard-nav',
      )}
      role="listbox"
      data-testid="chat-panel-composer-suggestion"
    >
      {!isSlash && (
        <div className="chat-composer-suggestion__header" data-testid="chat-panel-composer-suggestion-header">
          <AtSign size={14} />
          <span>{t('chat.selectTeamMembers')}</span>
        </div>
      )}
      <div
        ref={listRef}
        className="chat-composer-suggestion__list"
        data-testid="chat-panel-composer-suggestion-list"
        style={isSlash && slashListMaxHeight != null ? { maxHeight: slashListMaxHeight } : undefined}
      >
        {items.length === 0 ? (
          <div className="chat-composer-suggestion__empty" data-testid="chat-panel-composer-suggestion-empty">
            {isSlash
              ? loading
                ? slashSkillsOnly
                  ? '正在加载技能…'
                  : '正在加载指令与技能…'
                : slashSkillsOnly
                  ? '没有匹配的技能'
                  : '没有匹配的指令或技能'
              : t('chat.noTeamMembersAvailable')}
          </div>
        ) : items.map((item, index) => {
          const showSectionTitle = isSlash && (
            index === 0 || items[index - 1]?.itemKind !== item.itemKind
          );
          const sectionCount = item.itemKind === 'command' ? commandCount : skillCount;
          return (
            <Fragment key={`${suggestion.kind}:${item.itemKind}:${item.id}`}>
              {showSectionTitle && (
                <div className="chat-composer-suggestion__section-title">
                  <span>{item.itemKind === 'command' ? '指令' : '技能'}</span>
                  <span>({sectionCount})</span>
                </div>
              )}
              <button
                ref={highlightedIndex === index ? activeItemRef : undefined}
                type="button"
                className={clsx(
                  'chat-composer-suggestion__item',
                  !item.disabled && highlightedIndex === index && 'chat-composer-suggestion__item--active',
                  item.disabled && 'chat-composer-suggestion__item--disabled',
                )}
                role="option"
                aria-selected={!item.disabled && highlightedIndex === index}
                aria-disabled={item.disabled || undefined}
                disabled={item.disabled}
                title={item.disabledReason}
                data-testid="chat-panel-composer-suggestion-item"
                data-variant={item.id}
                onMouseDown={(event) => event.preventDefault()}
                onPointerMove={() => {
                  if (!item.disabled) onPointerHighlight(index);
                }}
                onClick={() => {
                  if (!item.disabled) {
                    onPick(
                      suggestion.kind,
                      item.id,
                      item.label,
                      item.itemKind,
                      item.takesArgs,
                    );
                  }
                }}
              >
                {isSlash ? (
                  <>
                    <span
                      className={clsx(
                        'chat-composer-suggestion__slash-icon',
                        item.itemKind === 'skill' && 'chat-composer-suggestion__slash-icon--skill',
                      )}
                      aria-hidden="true"
                    >
                      {item.itemKind === 'command' ? '/' : null}
                    </span>
                    <span className="chat-composer-suggestion__text">
                      <span className="chat-composer-suggestion__label">{item.label}</span>
                      {item.description ? (
                        <span className="chat-composer-suggestion__meta">{item.description}</span>
                      ) : null}
                    </span>
                  </>
                ) : (
                  <>
                    <span className="chat-composer-suggestion__avatar" aria-hidden="true">
                      <TeamMemberAvatar member={item.id} className="chat-composer-suggestion__team-avatar" />
                    </span>
                    <span className="chat-composer-suggestion__text">
                      <span className="chat-composer-suggestion__label">{item.label}</span>
                      <span className="chat-composer-suggestion__meta">
                        {`${tokenPrefix}${item.id}`}
                      </span>
                    </span>
                  </>
                )}
              </button>
            </Fragment>
          );
        })}
      </div>
    </div>
  );
}

function PermissionSelector({
  disabled = false,
  permissionsEnabled,
  onSavePermission,
}: {
  disabled?: boolean;
  permissionsEnabled: boolean;
  onSavePermission: (updates: Record<string, string>) => Promise<void>;
}) {
  const { t } = useTranslation();

  const permission: Permission = permissionsEnabled ? 'default' : 'full_access';

  const [isOpen, setIsOpen] = useState(false);
  const [menuDirection, setMenuDirection] = useState<'up' | 'down'>('up');
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const [pendingPermission, setPendingPermission] = useState<Permission | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuPortalRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const handler = (e: PointerEvent) => {
      if (
        !menuRef.current?.contains(e.target as Node) &&
        !menuPortalRef.current?.contains(e.target as Node)
      ) setIsOpen(false);
    };
    document.addEventListener('pointerdown', handler);
    return () => document.removeEventListener('pointerdown', handler);
  }, [isOpen]);

  const handleSelect = useCallback((value: Permission) => {
    setIsOpen(false);
    if (value === permission) return;
    if (value === 'full_access') {
      setPendingPermission('full_access');
    } else {
      onSavePermission({ permissions_enabled: 'true' });
    }
  }, [permission, onSavePermission]);

  const handleConfirm = useCallback(() => {
    if (pendingPermission) {
      onSavePermission({ permissions_enabled: 'false' });
    }
    setPendingPermission(null);
  }, [pendingPermission, onSavePermission]);

  const currentPerm = PERMISSION_OPTIONS.find((o) => o.value === permission) ?? PERMISSION_OPTIONS[0];

  return (
    <>
      <div
        ref={menuRef}
        className={clsx('chat-mode-select', isOpen && 'chat-mode-select--open')}
        data-testid="chat-panel-permission-selector-root"
      >
        <button
          type="button"
          className={clsx(
            'chat-mode-select__trigger',
            permission === 'full_access' && !disabled && 'chat-mode-select__trigger--danger',
          )}
          disabled={disabled}
          data-testid="chat-panel-permission-selector-trigger"
          data-variant={permission}
          title={disabled ? t('chat.configLockedHistory') : undefined}
          onClick={() => {
            if (disabled) return;
          if (!isOpen && menuRef.current) {
            const rect = menuRef.current.getBoundingClientRect();
            setMenuDirection(resolveMenuDirection(rect.bottom, 358));
            setMenuAnchor(rect);
          }
            setIsOpen((v) => !v);
          }}
          aria-haspopup="menu"
          aria-expanded={isOpen}
        >
          <span className="chat-mode-select__value">
            <span className="chat-mode-select__icon" aria-hidden="true">
              <currentPerm.icon className="w-4 h-4" />
            </span>
            <span className="chat-mode-select__label">{t(currentPerm.i18nKey)}</span>
          </span>
          <svg className="chat-mode-select__chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
          </svg>
        </button>

        {isOpen && menuAnchor && createPortal(
          <div
            ref={menuPortalRef}
            className="chat-mode-select__menu perm-select__menu"
            role="menu"
            data-testid="chat-panel-permission-selector-menu"
            style={menuDirection === 'up'
              ? { position: 'fixed', bottom: window.innerHeight - menuAnchor.top + 10, left: menuAnchor.left, zIndex: 9999 }
              : { position: 'fixed', top: menuAnchor.bottom + 10, left: menuAnchor.left, zIndex: 9999 }
            }
          >
            {PERMISSION_OPTIONS.map((opt) => (
              <button
                type="button"
                key={opt.value}
                onClick={() => handleSelect(opt.value)}
                className={clsx(
                  'chat-mode-select__option',
                  'perm-select__option',
                  permission === opt.value && 'chat-mode-select__option--active',
                )}
                role="menuitemradio"
                aria-checked={permission === opt.value}
                data-testid="chat-panel-permission-selector-option"
                data-variant={opt.value}
              >
                <span className="perm-select__option-main">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <opt.icon className="w-4 h-4" />
                  </span>
                  <span className="perm-select__text">
                    <span className="chat-mode-select__label">{t(opt.i18nKey)}</span>
                    {opt.descriptionI18nKey && (
                      <span className="perm-select__desc">{t(opt.descriptionI18nKey)}</span>
                    )}
                  </span>
                </span>
                {permission === opt.value && (
                  <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                  </svg>
                )}
              </button>
            ))}
          </div>,
          document.body
        )}
      </div>

      {pendingPermission === 'full_access' && (
        <PermissionWarningDialog
          onConfirm={handleConfirm}
          onCancel={() => setPendingPermission(null)}
        />
      )}
    </>
  );
}

function cx(...classes: (string | boolean | undefined | null)[]) {
  return classes.filter(Boolean).join(' ');
}
