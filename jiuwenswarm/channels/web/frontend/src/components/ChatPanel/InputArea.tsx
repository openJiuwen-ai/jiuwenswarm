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
} from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { AtSign, CircleX, ClipboardList, FileText, Loader2, Plus, Square, Target, X } from 'lucide-react';
import { useSpeechRecognition } from '../../hooks';

// import { stopAllTts } from '../../utils';
import {
  useChatStore,
  useGoalStore,
  usePlanStore,
  useSessionStore,
  useWorkspaceStore,
  resolveEffectiveModel,
} from '../../stores';
import { supportsPlanMode } from '../../features/planMode/wireMode';
import { queueOrAddGoalObjectiveMessage } from '../../features/goalPendingObjectiveBubble';
import { AgentMode, MediaItem, ModelEntry, Permission, type ProjectInfo } from '../../types';
import { NEW_CONVERSATION_ID } from '../../multi-session/state/newConversationLifecycle';
import { ProjectCreateMenu, type ProjectCreateMode } from '../../multi-session/sidebar/ProjectCreateMenu';
import { projectCreateErrorKey } from '../../multi-session/sidebar/projectCreateErrors';
import { AGENT_MODE_OPTIONS, PERMISSION_OPTIONS } from '../../config/chatConfig';
import clsx from 'clsx';
import { PermissionWarningDialog } from './PermissionWarningDialog';
import { ModelProviderIcon } from '../ModelProviderIcon';
import { FileIcon } from '../FileIcon';
import { getEvolutionPillLabel } from './evolution-status';
import { webRequest } from '../../services/webClient';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { withUploadDocumentBlock } from '../../utils/documentMessage';
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
import { getInputProjectOptions, isDefaultInputProject } from './projectSelection';
import sendIcon from '../../assets/send.svg';
import sendActiveIcon from '../../assets/send_active.svg';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { CodeBranchSelector } from '../../features/code-mode/CodeBranchSelector';
import { generateUuidV4 } from '../../utils/uuid';


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
};

/** 已安装插件信息（用于判定技能是否已安装） */
type InputAreaInstalledPlugin = {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit?: string | null;
  skills: string[];
};

type InputAreaTeamMember = {
  member_id: string;
  name?: string;
  status?: string;
};

type ComposerSuggestionKind = 'member' | 'role';
type WorkIconName = 'add' | 'arrow' | 'check' | 'close' | 'collapse' | 'expand' | 'folder' | 'search';

type ComposerSuggestionState = {
  kind: ComposerSuggestionKind;
  query: string;
};

type ComposerSuggestionItem = {
  id: string;
  label: string;
  status?: string;
};

function getComposerSuggestionItems(
  suggestion: ComposerSuggestionState | null,
  members: ComposerSuggestionItem[]
): ComposerSuggestionItem[] {
  if (!suggestion) return [];
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
  onPersistMedia: (content: string, mediaItems: MediaItem[]) => Promise<PersistMediaResponse>;
  onPersistDocuments: (content: string, mediaItems: MediaItem[]) => Promise<PersistMediaResponse>;
  onInterrupt: (newInput?: string) => void;
  onCancel: () => void;
  onSwitchMode: (mode: AgentMode) => void;
  isProcessing: boolean;
  autoFocusKey?: string | null;
  /** 跳转到技能管理页 */
  onNavigateToSkills?: () => void;
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
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
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

function getImageValidationError(file: File): string | null {
  if (!isImageFile(file)) {
    return `文件类型不支持：${file.name || '未命名文件'}`;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    return `文件大小超出限制：${file.name || '未命名文件'}（最大${formatAttachmentSize(MAX_IMAGE_BYTES)}）`;
  }
  return null;
}

function clearAttachmentAlertTimers(timers: Map<string, number>): void {
  timers.forEach((timeoutId) => window.clearTimeout(timeoutId));
  timers.clear();
}

function getDocumentValidationError(
  file: File | undefined,
  options?: { filename?: string; localPath?: string },
): string | null {
  const filename = options?.filename || file?.name || '未命名文件';
  if (file && isForbiddenDocumentFile(file)) {
    return `禁止上传该文件类型：${filename}`;
  }
  if (file && !isDocumentFile(file)) {
    return `文件类型不支持：${filename}`;
  }
  if (!getLocalFilePath(file, options?.localPath)) {
    return `无法获取本地文件路径：${filename}（请使用桌面端选择文件）`;
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

function readImageFile(file: File): Promise<Pick<AttachmentDraft, 'base64Data' | 'previewUrl'> | null> {
  if (getImageValidationError(file)) {
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
    onPersistMedia,
    onPersistDocuments,
    onInterrupt,
    onCancel,
    onSwitchMode,
    isProcessing,
    autoFocusKey = null,
    onNavigateToSkills,
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
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [workMenuOpen, setWorkMenuOpen] = useState<'project' | null>(null);
  const [workDialogOpen, setWorkDialogOpen] = useState(false);
  const [projectNameDraft, setProjectNameDraft] = useState('');
  const [projectDirDraft, setProjectDirDraft] = useState('');
  const [projectDirError, setProjectDirError] = useState<string | null>(null);
  const [projectSearch, setProjectSearch] = useState('');
  const [projectCreateMode, setProjectCreateMode] = useState<ProjectCreateMode>('blank');
  const [menuDirection, setMenuDirection] = useState<'up' | 'down'>('up');
  const [hoveredOptionDesc, setHoveredOptionDesc] = useState<string | null>(null);

  useEffect(() => {
    if (!projectDirError || workDialogOpen) return;
    const timeoutId = window.setTimeout(() => setProjectDirError(null), 3000);
    return () => window.clearTimeout(timeoutId);
  }, [projectDirError, workDialogOpen]);

  const [composerSuggestion, setComposerSuggestion] = useState<ComposerSuggestionState | null>(null);
  const [composerSuggestionIndex, setComposerSuggestionIndex] = useState(0);
  const [modeMenuAnchor, setModeMenuAnchor] = useState<DOMRect | null>(null);
  const [attachMenuOpen, setAttachMenuOpen] = useState(false);
  const [attachMenuAnchor, setAttachMenuAnchor] = useState<DOMRect | null>(null);
  const inputRef = useRef<HTMLDivElement>(null);
  /** 保存技能插入前的光标位置，用于在光标处插入 chip */
  const savedRangeRef = useRef<Range | null>(null);
  const modeMenuRef = useRef<HTMLDivElement>(null);
  const workMenuRef = useRef<HTMLDivElement>(null);
  const modeMenuPortalRef = useRef<HTMLDivElement>(null);
  const attachMenuRef = useRef<HTMLDivElement>(null);
  const attachMenuPortalRef = useRef<HTMLDivElement>(null);
  const autoSendTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attachmentMenuTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attachmentMenuOpenedByLongPressRef = useRef(false);
  const isComposingRef = useRef(false);
  // const activePointerIdRef = useRef<number | null>(null);
  const isVoicePressingRef = useRef(false);
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const isPaused = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.isPaused ?? false);
  const queuePaused = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.queuePaused ?? false);
  const isLoadingHistory = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.isLoadingHistory ?? false);
  const inputValue = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.inputValue ?? '');
  const evolutionStatus = useChatStore((s) => s.runtimes[activeSessionId ?? '']?.evolutionStatus ?? null);
  const mode = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.mode ?? 'agent');
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
  const hasUnfinishedGoal = currentGoal != null && currentGoal.status !== 'completed';
  const isInterruptible = isProcessing || isPaused || isGoalActive;
  const isAgentMode = mode === 'agent';
  const isTeamMode = mode === 'team';
  const isAutoHarnessMode = mode === 'auto_harness';
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

  const composerSuggestionItems = useMemo(
    () => getComposerSuggestionItems(composerSuggestion, mentionableMembers),
    [composerSuggestion, mentionableMembers]
  );

  useEffect(() => {
    setComposerSuggestionIndex(0);
  }, [composerSuggestion?.kind, composerSuggestion?.query]);

  useEffect(() => {
    if (composerSuggestionItems.length === 0) {
      setComposerSuggestionIndex(0);
      return;
    }
    setComposerSuggestionIndex((index) => Math.min(index, composerSuggestionItems.length - 1));
  }, [composerSuggestionItems.length]);

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
      console.error('语音识别错误:', error);
    },
  });

  const imageInputDisabled = isListening || (isInterruptible && !isTeamMode);
  const isDesktopBridgeReady = useDesktopLocalFilePickerReady();
  // "+" 触发按钮本身不跟图片/目标的可用性挂钩：菜单以后可能挂其他跟图片/目标无关的功能，
  // 触发按钮只要不在录音就该能点开；具体某一项能不能选，交给菜单里每一项各自的禁用态处理。
  const attachTriggerDisabled = isListening;
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

        setTimeout(() => {
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
  }, [isListening, pendingVoiceText, inputValue, isInterruptible, isTeamMode, onSubmit, onInterrupt]);

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

  const startAttachmentMenuTimer = useCallback((id: string) => {
    stopAttachmentMenuTimer();
    attachmentMenuOpenedByLongPressRef.current = false;
    attachmentMenuTimerRef.current = setTimeout(() => {
      attachmentMenuOpenedByLongPressRef.current = true;
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
        ? getDocumentValidationError(attachment.file, {
            filename: attachment.filename,
            localPath: attachment.localPath,
          })
        : attachment.file
          ? getImageValidationError(attachment.file)
          : (attachment.base64Data ? null : `文件类型不支持：${attachment.filename || '未命名文件'}`);
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
        const error = `无法获取本地文件路径：${attachment.filename || '未命名文件'}`;
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
          console.error('文档上传失败:', error);
          updateAttachment(attachment.id, {
            status: 'error',
            error: '上传失败，请重试',
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
          console.error('图片上传失败:', error);
          updateAttachment(attachment.id, {
            ...payload,
            status: 'error',
            error: '上传失败，请重试',
          });
        }
      })();
      return;
    }

    if (!attachment.file) {
      const error = '上传失败，请重试';
      pushAttachmentAlert(error);
      updateAttachment(attachment.id, { status: 'error', error });
      return;
    }

    void readImageFile(attachment.file).then(async (payload) => {
      if (!payload) {
        updateAttachment(attachment.id, {
          status: 'error',
          error: '上传失败，请重试',
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
        console.error('图片上传失败:', error);
        updateAttachment(attachment.id, {
          ...payload,
          status: 'error',
          error: '上传失败，请重试',
        });
      }
    });
  }, [canPersistAttachments, onPersistDocuments, onPersistMedia, pushAttachmentAlert, updateAttachment]);

  const retryAttachment = useCallback((attachment: AttachmentDraft) => {
    uploadAttachment(attachment);
  }, [uploadAttachment]);

  const appendAttachmentFiles = useCallback((files: FileList | File[]) => {
    const selectedFiles = Array.from(files);
    if (!selectedFiles.length) return;

    const drafts = selectedFiles.reduce<AttachmentDraft[]>((items, file) => {
      const kind = resolveAttachmentKind(file);
      if (!kind) {
        const message = isForbiddenDocumentFile(file)
          ? `禁止上传该文件类型：${file.name || '未命名文件'}`
          : `文件类型不支持：${file.name || '未命名文件'}`;
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
          ? getDocumentValidationError(file, { filename: base.filename, localPath })
          : getImageValidationError(file);
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
  }, [pushAttachmentAlert, uploadAttachment]);

  const appendLocalFilePicks = useCallback((picks: LocalFilePick[]) => {
    if (!picks.length) return;

    const drafts = picks.reduce<AttachmentDraft[]>((items, pick) => {
      if (pick.error === 'forbidden') {
        pushAttachmentAlert(`禁止上传该文件类型：${pick.filename}`);
        return items;
      }
      if (pick.error === 'image_too_large') {
        pushAttachmentAlert(
          `文件大小超出限制：${pick.filename}（最大${formatAttachmentSize(MAX_IMAGE_BYTES)}）`,
        );
        return items;
      }
      if (pick.error === 'read_failed') {
        pushAttachmentAlert(`读取文件失败：${pick.filename}`);
        return items;
      }
      if (pick.kind === 'image' && !pick.base64) {
        pushAttachmentAlert(`读取图片失败：${pick.filename}`);
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
  }, [pushAttachmentAlert, uploadAttachment]);

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
        ? '当前环境无法打开本机文件选择器（请确认 jiuwenswarm 在本机运行且已重启加载最新后端）'
        : (result.message || '打开文件选择器失败');
    pushAttachmentAlert(hint);
  }, [appendLocalFilePicks, imageInputDisabled, pushAttachmentAlert]);

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
        !attachMenuPortalRef.current?.contains(event.target as Node)
      ) {
        setAttachMenuOpen(false);
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

  /** 从 contenteditable 提取纯文本（技能 chip 不进入纯文本，其它 token 展开为 @/$ 文本） */
  const extractPlainText = useCallback((): string => {
    const el = inputRef.current;
    if (!el) return '';
    let text = '';
    el.childNodes.forEach((node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent || '';
      } else if (node.nodeType === Node.ELEMENT_NODE) {
        const elem = node as HTMLElement;
        if (elem.getAttribute('contenteditable') === 'false' && elem.dataset.composerToken) {
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
        if (elem.getAttribute('contenteditable') === 'false' && elem.hasAttribute('data-skill')) {
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

  const handleSubmit = useCallback(() => {
    // 用富文本（含 chip 标记）作为发送内容，气泡可交织渲染技能
    const richContent = extractRichContent();
    const trimmedBase = (richContent + pendingVoiceText).trim();
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
    extractRichContent,
    pendingVoiceText,
    readyMediaItems,
    hasUploadingAttachments,
    hasAttachmentErrors,
    isInterruptible,
    isListening,
    onSubmit,
    onInterrupt,
    stopListening,
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
  // Attachments / listening still count as "composer busy" so Stop stays hidden
  // while the user is preparing a follow-up, but they do not enable Send.
  const hasDraft = hasTextDraft || attachments.length > 0 || isListening;
  const isImageInterruptBlocked =
    isInterruptible && !isTeamMode && !isAgentMode && readyMediaItems.length > 0;
  const showStop = isProcessing && !isPaused && !hasDraft;
  const hasReadyMedia = readyMediaItems.length > 0;
  const canSubmit = showStop || (
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
    const match = beforeText.match(/([@$])([\p{L}\p{N}_\-\u4e00-\u9fa5]*)$/u);
    if (!match) return null;

    return {
      kind: match[1] === '@' ? 'member' : 'role',
      query: match[2] || '',
    };
  }, []);

  const updateComposerSuggestion = useCallback(() => {
    const trigger = getCurrentComposerTrigger();
    if (!trigger || mentionableMembers.length === 0) {
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

  const insertComposerToken = useCallback((kind: ComposerSuggestionKind, value: string, label: string) => {
    const el = inputRef.current;
    const selection = window.getSelection();
    if (!el || !selection || selection.rangeCount === 0) return;
    const range = selection.getRangeAt(0);
    if (!el.contains(range.commonAncestorContainer)) return;

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
  }, [extractPlainText, getCurrentComposerTrigger, setRangeStartByTextOffset]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (composerSuggestion) {
        if (e.key === 'Escape') {
          e.preventDefault();
          setComposerSuggestion(null);
          return;
        }

        if (e.key === 'ArrowDown') {
          e.preventDefault();
          if (composerSuggestionItems.length > 0) {
            setComposerSuggestionIndex((index) => (index + 1) % composerSuggestionItems.length);
          }
          return;
        }

        if (e.key === 'ArrowUp') {
          e.preventDefault();
          if (composerSuggestionItems.length > 0) {
            setComposerSuggestionIndex((index) => (
              index - 1 + composerSuggestionItems.length
            ) % composerSuggestionItems.length);
          }
          return;
        }

        if ((e.key === 'Enter' || e.key === 'Tab') && !e.shiftKey) {
          if (isComposingRef.current || e.nativeEvent.isComposing) return;
          e.preventDefault();
          const item = composerSuggestionItems[composerSuggestionIndex];
          if (item) {
            insertComposerToken(composerSuggestion.kind, item.id, item.label);
          }
          return;
        }
      }

      if (e.key !== 'Enter' || e.shiftKey) return;
      if (isComposingRef.current || e.nativeEvent.isComposing) return;
      e.preventDefault();
      handleSubmit();
    },
    [
      composerSuggestion,
      composerSuggestionIndex,
      composerSuggestionItems,
      handleSubmit,
      insertComposerToken,
    ]
  );

  /** contenteditable 输入时同步纯文本到 store + 联动 selectedSkills */
  const handleEditorInput = useCallback(() => {
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    // 提取纯文本
    const text = extractPlainText();
    useChatStore.getState().setInputValue(sid, text);
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
  }, [extractPlainText, updateComposerSuggestion]);

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
      const imageFiles = hasBrowserFiles
        ? Array.from(event.clipboardData?.items || [])
            .filter((item) => item.kind === 'file')
            .map((item) => item.getAsFile())
            .filter((file): file is File => Boolean(file && isImageFile(file)))
        : [];

      if (hasBrowserFiles) {
        event.preventDefault();
        if (imageInputDisabled) return true;
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
    [appendAttachmentFiles, appendLocalFilePicks, imageInputDisabled, isDesktopBridgeReady],
  );

  const handlePaste = useCallback(
    (event: ClipboardEvent<HTMLDivElement>) => {
      if (handleDesktopFilePaste(event)) return;
      if (clipboardHasFileItems(event.clipboardData)) {
        event.preventDefault();
      }
    },
    [handleDesktopFilePaste],
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
    await handleModeSwitch(targetMode);
  }, [handleModeSwitch]);

  useEffect(() => {
    setIsModeMenuOpen(false);
  }, [isProcessing, mode]);

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
      <div className="chat-input-frame" data-testid="chat-panel-input-frame">
        <div
          className={cx(
            'chat-input-container',
            showWorkContextRow && 'chat-input-container--work-home',
            (isModeMenuOpen || workMenuOpen) && 'chat-input-container--menu-open',
            composerSuggestion && 'chat-input-container--suggestion-open',
            isListening && 'chat-input-container--recording',
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
                        <span data-testid="chat-panel-input-attachment-status" data-variant="uploading">上传中...</span>
                      </>
                    ) : attachment.status === 'error' ? (
                      <>
                        <span
                          className="chat-input-attachment-status-error"
                          data-testid="chat-panel-input-attachment-status"
                          data-variant="error"
                          title={attachment.error || '上传失败'}
                        >
                          上传失败
                        </span>
                        {attachment.file && (
                          <button
                            type="button"
                            className="chat-input-attachment-retry"
                            data-testid="chat-panel-input-attachment-retry"
                            onClick={() => retryAttachment(attachment)}
                          >
                            重试
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
                  onPointerDown={() => startAttachmentMenuTimer(attachment.id)}
                  onPointerUp={stopAttachmentMenuTimer}
                  onPointerCancel={stopAttachmentMenuTimer}
                  onPointerLeave={stopAttachmentMenuTimer}
                  onContextMenu={(event) => {
                    event.preventDefault();
                    stopAttachmentMenuTimer();
                    setAttachmentMenuId(attachment.id);
                  }}
                  onClick={() => handleAttachmentRemoveClick(attachment.id)}
                  title="删除，长按显示更多操作"
                  aria-label="删除附件"
                >
                  <X size={12} strokeWidth={2} />
                </button>
                {attachmentMenuId === attachment.id && (
                  <div className="chat-input-attachment-menu" role="menu" data-testid="chat-panel-input-attachment-menu">
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="chat-panel-input-attachment-menu-delete"
                      onClick={() => removeAttachment(attachment.id)}
                    >
                      删除
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      data-testid="chat-panel-input-attachment-menu-clear"
                      onClick={clearAttachments}
                    >
                      清空附件
                    </button>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {composerSuggestion && (
        <ComposerSuggestionMenu
          suggestion={composerSuggestion}
          items={composerSuggestionItems}
          highlightedIndex={composerSuggestionIndex}
          onHighlight={setComposerSuggestionIndex}
          onPick={insertComposerToken}
        />
      )}
      <div
        ref={inputRef}
        contentEditable
        suppressContentEditableWarning
        onInput={handleEditorInput}
        onKeyDown={handleKeyDown}
        onCompositionStart={() => { isComposingRef.current = true; }}
        onCompositionEnd={() => { isComposingRef.current = false; }}
        onBlur={saveSelection}
        onPaste={handlePaste}
        data-placeholder={
          isListening
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
        className="chat-input-editor"
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
                  setAttachMenuAnchor(attachMenuRef.current.getBoundingClientRect());
                }
                setAttachMenuOpen((open) => !open);
              }}
              disabled={attachTriggerDisabled}
              className={cx(
                'chat-input-btn chat-input-btn--add-file',
                attachTriggerDisabled && 'chat-input-btn--disabled',
              )}
              title={attachTriggerDisabled ? t('chat.addFileDisabled') : t('chat.addFile')}
              aria-label={attachTriggerDisabled ? t('chat.addFileDisabled') : t('chat.addFile')}
              aria-haspopup="menu"
              aria-expanded={attachMenuOpen}
            >
              <Plus className="chat-input-btn-icon" strokeWidth={1.8} />
            </button>
            {attachMenuOpen && attachMenuAnchor && createPortal(
              <div
                ref={attachMenuPortalRef}
                className="chat-mode-select__menu"
                role="menu"
                data-testid="chat-panel-input-attach-menu"
                style={{
                  position: 'fixed',
                  bottom: window.innerHeight - attachMenuAnchor.top + 10,
                  left: attachMenuAnchor.left,
                  zIndex: 9999,
                }}
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
                    <span className="chat-mode-select__icon" aria-hidden="true">
                      <FileText className="w-4 h-4" />
                    </span>
                    <span className="chat-mode-select__label">{t('chat.addFile')}</span>
                  </span>
                </button>
                {canUseGoalMenu && (() => {
                  // Goal 和 Plan 互斥：已有真正生效的目标/计划时都不能再选目标。已提交的目标沿用
                  // 原提示；被"计划已生效"挡住时换一条对应文案，避免误导用户去找目标本身的问题。
                  const goalDisabled = hasUnfinishedGoal || planCommitted;
                  const goalDisabledTitle = hasUnfinishedGoal
                    ? t('goal.toolbarUnavailable')
                    : planCommitted
                      ? t('goal.toolbarUnavailablePlan')
                      : undefined;
                  return (
                    <button
                      type="button"
                      className="chat-mode-select__option"
                      role="menuitem"
                      data-testid="chat-panel-input-attach-menu-goal"
                      disabled={goalDisabled}
                      title={goalDisabledTitle}
                      onClick={() => {
                        if (goalDisabled) return;
                        setAttachMenuOpen(false);
                        if (activeSessionId) {
                          // 走到这里 planCommitted 一定是 false（否则上面已 disabled），所以 planActive
                          // 为 true 时只可能是"刚打开开关、还没发过消息"的未提交态，可以放心顶掉。
                          if (planActive) {
                            usePlanStore.getState().setActive(activeSessionId, false);
                          }
                          useGoalStore.getState().setArmed(activeSessionId, true);
                        }
                      }}
                    >
                      <span className="chat-mode-select__option-main">
                        <span className="chat-mode-select__icon" aria-hidden="true">
                          <Target className="w-4 h-4" />
                        </span>
                        <span className="chat-mode-select__label">{t('goal.toolbarTag')}</span>
                      </span>
                    </button>
                  );
                })()}
                {canUsePlanMenu && (() => {
                  // 对称地：已有未完成目标时不能选计划；对话进行中（isProcessing）时也先禁掉，
                  // 避免在当前这轮还没结束时又叠加切一次模式。
                  const planDisabled = hasUnfinishedGoal || isProcessing;
                  const planDisabledTitle = hasUnfinishedGoal
                    ? t('plan.toolbarUnavailableGoal')
                    : isProcessing
                      ? t('plan.toolbarUnavailableProcessing')
                      : undefined;
                  return (
                    <button
                      type="button"
                      className="chat-mode-select__option"
                      role="menuitem"
                      data-testid="chat-panel-input-attach-menu-plan"
                      disabled={planDisabled}
                      title={planDisabledTitle}
                      onClick={() => {
                        if (planDisabled) return;
                        setAttachMenuOpen(false);
                        if (activeSessionId) {
                          // 走到这里 hasUnfinishedGoal 一定是 false，goalArmed 为 true 时只可能是
                          // "刚选了目标、还没发消息"的未提交态，顶掉换成 Plan。
                          useGoalStore.getState().setArmed(activeSessionId, false);
                          // explicitEntry：这是用户手动打开开关，下一条 Plan 消息要带
                          // plan_entry_source，否则会被后端的防重入闸门拦下。
                          usePlanStore
                            .getState()
                            .setActive(activeSessionId, true, { explicitEntry: true });
                        }
                      }}
                    >
                      <span className="chat-mode-select__option-main">
                        <span className="chat-mode-select__icon" aria-hidden="true">
                          <ClipboardList className="w-4 h-4" />
                        </span>
                        <span className="chat-mode-select__label">{t('plan.toolbarTag')}</span>
                      </span>
                    </button>
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
                  const spaceBelow = window.innerHeight - rect.bottom;
                  const dir = spaceBelow >= 120 ? 'down' : 'up';
                  setMenuDirection(dir);
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
                className="chat-mode-select__menu"
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
                    onMouseEnter={() => setHoveredOptionDesc(m.descriptionI18nKey ?? null)}
                    onMouseLeave={() => setHoveredOptionDesc(null)}
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
            {isModeMenuOpen && hoveredOptionDesc && modeMenuAnchor && createPortal(
              <div
                className="chat-mode-option-tooltip"
                data-testid="chat-panel-mode-select-tooltip"
                style={menuDirection === 'up'
                  ? { position: 'fixed', bottom: window.innerHeight - modeMenuAnchor.top + 10, left: modeMenuAnchor.left + 188, zIndex: 10000 }
                  : { position: 'fixed', top: modeMenuAnchor.bottom + 10, left: modeMenuAnchor.left + 188, zIndex: 10000 }
                }
              >
                {t(hoveredOptionDesc)}
              </div>,
              document.body
            )}
          </div>
          <PermissionSelector permissionsEnabled={permissionsEnabled} onSavePermission={onSavePermission} />

          {!isTeamMode && <SkillSelector
            onNavigateToSkills={onNavigateToSkills}
            onInsertSkill={insertSkillChip}
            onRemoveSkill={removeSkillChip}
          />}

          {goalTagVisible && (
            <div className="chat-goal-tag" data-testid="chat-panel-goal-tag">
              <button type="button" className="chat-mode-select__trigger" data-testid="chat-panel-goal-tag-label">
                <span className="chat-mode-select__value">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <Target className="w-4 h-4" />
                  </span>
                  <span className="chat-mode-select__label">{t('goal.toolbarTag')}</span>
                </span>
              </button>
              <button
                type="button"
                className="chat-goal-tag__close"
                data-testid="chat-panel-goal-tag-close"
                title={t('goal.closeTag')}
                onClick={() => {
                  if (!activeSessionId) return;
                  if (currentGoal) {
                    onClearGoal?.(activeSessionId);
                  }
                  useGoalStore.getState().setArmed(activeSessionId, false);
                }}
              >
                <X size={11} strokeWidth={2.5} />
              </button>
            </div>
          )}

          {planTagVisible && (
            <div className="chat-goal-tag" data-testid="chat-panel-plan-tag">
              <button type="button" className="chat-mode-select__trigger" data-testid="chat-panel-plan-tag-label">
                <span className="chat-mode-select__value">
                  <span className="chat-mode-select__icon" aria-hidden="true">
                    <ClipboardList className="w-4 h-4" />
                  </span>
                  <span className="chat-mode-select__label">{t('plan.toolbarTag')}</span>
                </span>
              </button>
              <button
                type="button"
                className="chat-goal-tag__close"
                data-testid="chat-panel-plan-tag-close"
                disabled={isProcessing}
                title={isProcessing ? t('plan.closeTagDisabled') : t('plan.closeTag')}
                onClick={() => {
                  if (isProcessing) return;
                  if (!activeSessionId) return;
                  usePlanStore.getState().setActive(activeSessionId, false);
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

          <ModelSelector
            disabled={isTeamMode || isProcessing}
            lockedToDefault={isTeamMode}
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

      {showWorkContextRow ? (
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
              <svg className="chat-work-select__chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
              </svg>
            </button>
            {displayedProject && !isWorkContextLocked ? (
              <span className="chat-work-select__clear-wrap" aria-hidden="false">
                <button
                  type="button"
                  className="chat-work-select__clear"
                  data-testid="chat-panel-work-select-clear"
                  aria-label={t('multiSession.project.clearProject')}
                  data-tooltip={t('multiSession.project.clearProject')}
                  onClick={() => {
                    setSelectedProject(null);
                    setWorkMenuOpen(null);
                  }}
                >
                  <WorkIcon name="close" />
                </button>
              </span>
            ) : null}
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
  onHighlight,
  onPick,
}: {
  suggestion: ComposerSuggestionState;
  items: ComposerSuggestionItem[];
  highlightedIndex: number;
  onHighlight: (index: number) => void;
  onPick: (kind: ComposerSuggestionKind, value: string, label: string) => void;
}) {
  const tokenPrefix = suggestion.kind === 'role' ? '$' : '@';

  return (
    <div className="chat-composer-suggestion" role="listbox" data-testid="chat-panel-composer-suggestion">
      <div className="chat-composer-suggestion__header" data-testid="chat-panel-composer-suggestion-header">
        <AtSign size={14} />
        <span>选择团队成员</span>
      </div>
      <div className="chat-composer-suggestion__list" data-testid="chat-panel-composer-suggestion-list">
        {items.length === 0 ? (
          <div className="chat-composer-suggestion__empty" data-testid="chat-panel-composer-suggestion-empty">
            暂无可选择的团队成员
          </div>
        ) : items.map((item, index) => (
          <button
            key={`${suggestion.kind}:${item.id}`}
            type="button"
            className={clsx(
              'chat-composer-suggestion__item',
              highlightedIndex === index && 'chat-composer-suggestion__item--active'
            )}
            role="option"
            aria-selected={highlightedIndex === index}
            data-testid="chat-panel-composer-suggestion-item"
            data-variant={item.id}
            onMouseDown={(event) => event.preventDefault()}
            onMouseEnter={() => onHighlight(index)}
            onClick={() => onPick(suggestion.kind, item.id, item.label)}
          >
            <span className="chat-composer-suggestion__avatar" aria-hidden="true">
              <TeamMemberAvatar member={item.id} className="chat-composer-suggestion__team-avatar" />
            </span>
            <span className="chat-composer-suggestion__text">
              <span className="chat-composer-suggestion__label">{item.label}</span>
              <span className="chat-composer-suggestion__meta">
                {`${tokenPrefix}${item.id}`}
              </span>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function ModelSelector({
  disabled = false,
  lockedToDefault = false,
}: {
  disabled?: boolean;
  lockedToDefault?: boolean;
}) {
  const chatAvailableModels = useSessionStore((s) => s.chatAvailableModels);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const selectedModelName = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.selectedModelName ?? null);
  const defaultModelName = useSessionStore((s) => s.defaultModelName);
  const setSelectedModelName = useSessionStore((s) => s.setSelectedModelName);
  const { t } = useTranslation();

  const [isOpen, setIsOpen] = useState(false);
  const [menuDirection, setMenuDirection] = useState<'up' | 'down'>('up');
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
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

  if (chatAvailableModels.length === 0) return null;

  // 集群模式下 UI 禁止手动改模型（见下方 disabled/tooltip），但显示仍应优先反映
  // 该会话实际记录的模型（如定时任务在集群模式下显式指定了非默认模型，后端也确实
  // 按该模型执行——见 bug002 回归），而不是不管三七二十一恒显示全局默认模型；
  // 从未指定过模型的会话 selectedModelName 本就兜底等于默认模型，行为不变。
  // 与实际发给后端的 model_name（sessionStore.getEffectiveModelName）复用同一套解析逻辑，
  // 避免模型改名/改别名后 UI 显示值和实际请求参数走出两份不同的兜底结果（bug003）。
  const selectedModel =
    resolveEffectiveModel(chatAvailableModels, selectedModelName, defaultModelName) ??
    chatAvailableModels[0];

  const handleSelect = (modelKey: string) => {
    setIsOpen(false);
    if (activeSessionId) setSelectedModelName(activeSessionId, modelKey);
  };

  const handleAddModel = () => {
    setIsOpen(false);
    window.dispatchEvent(new CustomEvent<string>('jiuwen:nav', { detail: 'configpanel' }));
  };

  return (
    <div
      ref={menuRef}
      className={clsx('chat-mode-select', isOpen && 'chat-mode-select--open')}
      data-testid="chat-panel-model-selector-root"
    >
      <button
        type="button"
        className="chat-mode-select__trigger"
        title={t(lockedToDefault ? 'chat.modelSelector.clusterLockedTooltip' : 'chat.modelSelector.tooltip')}
        onClick={() => {
          if (disabled) return;
          if (!isOpen && menuRef.current) {
            const rect = menuRef.current.getBoundingClientRect();
            setMenuDirection(window.innerHeight - rect.bottom >= 200 ? 'down' : 'up');
            setMenuAnchor(rect);
          }
          setIsOpen((v) => !v);
        }}
        style={disabled ? { cursor: 'default' } : undefined}
        aria-disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        data-testid="chat-panel-model-selector-trigger"
      >
        <span className="chat-mode-select__value">
          <span className="chat-mode-select__icon" aria-hidden="true">
            <ModelProviderIcon model={selectedModel} />
          </span>
          <span className="chat-mode-select__label">
            {selectedModel.alias || selectedModel.model_name}
          </span>
        </span>
        {!disabled && (
          <svg className="chat-mode-select__chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
          </svg>
        )}
      </button>

      {isOpen && menuAnchor && createPortal(
        <div
          ref={menuPortalRef}
          className="chat-mode-select__menu model-select__menu"
          role="menu"
          data-testid="chat-panel-model-selector-menu"
          style={menuDirection === 'up'
            ? { position: 'fixed', bottom: window.innerHeight - menuAnchor.top + 10, left: menuAnchor.left, zIndex: 9999 }
            : { position: 'fixed', top: menuAnchor.bottom + 10, left: menuAnchor.left, zIndex: 9999 }
          }
        >
          {(() => {
            const isFree = (m: ModelEntry) => m.is_free === true;
            const freeModels = chatAvailableModels.filter(isFree);
            const configuredModels = chatAvailableModels.filter((m) => !isFree(m));
            const renderGroup = (label: string, models: ModelEntry[]) =>
              models.length === 0 ? null : (
                <>
                  <div className="model-select__section-header" data-testid="chat-panel-model-selector-section-header">{label}</div>
                  {models.map((m, idx) => {
                    const key = m.alias || m.model_name;
                    const isActive = key === (selectedModel.alias || selectedModel.model_name);
                    return (
                      <button
                        type="button"
                        key={`${m.model_name}-${idx}`}
                        onClick={() => handleSelect(key)}
                        className={clsx(
                          'chat-mode-select__option',
                          isActive && 'chat-mode-select__option--active',
                        )}
                        role="menuitemradio"
                        aria-checked={isActive}
                        data-testid="chat-panel-model-selector-option"
                        data-variant={key}
                      >
                        <span className="chat-mode-select__option-main">
                          <span className="chat-mode-select__icon" aria-hidden="true">
                            <ModelProviderIcon model={m} />
                          </span>
                          <span className="chat-mode-select__label">{key}</span>
                        </span>
                        {isActive && (
                          <svg className="chat-mode-select__check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                          </svg>
                        )}
                      </button>
                    );
                  })}
                </>
              );
            return (
              <>
                {renderGroup(t('chat.modelSelector.free'), freeModels)}
                {renderGroup(t('chat.modelSelector.configured'), configuredModels)}
              </>
            );
          })()}
          <button
            type="button"
            className="model-select__add-btn"
            data-testid="chat-panel-model-selector-add"
            onClick={handleAddModel}
          >
            <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2} width={14} height={14} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M10 4v12M4 10h12" />
            </svg>
            {t('chat.modelSelector.addModel')}
          </button>
        </div>,
        document.body
      )}
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
              setMenuDirection(window.innerHeight - rect.bottom >= 160 ? 'down' : 'up');
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

/** 输入栏右侧的「技能」下拉，展示已安装技能（结构与技能页卡片保持一致） */
function SkillSelector({ onNavigateToSkills, onInsertSkill, onRemoveSkill }: {
  onNavigateToSkills?: () => void;
  onInsertSkill?: (skillName: string) => void;
  onRemoveSkill?: (skillName: string) => void;
}) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const selectedSkills = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.selectedSkills ?? []);
  const [isOpen, setIsOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [skills, setSkills] = useState<InputAreaSkillItem[]>([]);
  const [plugins, setPlugins] = useState<InputAreaInstalledPlugin[]>([]);
  const [searchQuery, setSearchQuery] = useState('');
  const menuRef = useRef<HTMLDivElement>(null);

  const installedSkillMap = useMemo(() => {
    const map = new Map<string, InputAreaInstalledPlugin>();
    plugins.forEach((plugin) => {
      plugin.skills.forEach((skillName) => {
        if (!map.has(skillName)) map.set(skillName, plugin);
      });
    });
    return map;
  }, [plugins]);

  const isSkillInstalled = useCallback(
    (skill: InputAreaSkillItem): boolean =>
      installedSkillMap.has(skill.name) ||
      skill.source === 'local' ||
      skill.source === 'project',
    [installedSkillMap],
  );

  const installedSkills = useMemo(
    () => skills.filter((s) => isSkillInstalled(s) && s.enabled !== false),
    [skills, isSkillInstalled],
  );

  // 按名称/描述过滤
  const filteredSkills = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return installedSkills;
    return installedSkills.filter((s) => {
      const name = s.name.toLowerCase();
      const displayName = (s.display_name || '').toLowerCase();
      const desc = (s.description || '').toLowerCase();
      return name.includes(q) || displayName.includes(q) || desc.includes(q);
    });
  }, [installedSkills, searchQuery]);

  const fetchInstalledSkills = useCallback(async () => {
    if (!activeSessionId) return;
    setLoading(true);
    setErrorMessage(null);
    try {
      const data = await webRequest<{
        skills?: InputAreaSkillItem[];
        plugins?: InputAreaInstalledPlugin[];
      }>(
        'skills.list',
        { with_installed: true },
        { timeoutMs: 30_000 },
      );
      setSkills(data.skills || []);
      setPlugins(data.plugins || []);
    } catch (err) {
      console.error('Failed to load installed skills:', err);
      setErrorMessage(t('skills.listError'));
    } finally {
      setLoading(false);
    }
  }, [activeSessionId, t]);

  useEffect(() => {
    if (isOpen) {
      void fetchInstalledSkills();
    } else {
      // 关闭时清空搜索词
      setSearchQuery('');
    }
  }, [isOpen, fetchInstalledSkills]);

  // 点击外部关闭下拉
  useEffect(() => {
    if (!isOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [isOpen]);

  const handleOpenSkillsPage = useCallback(() => {
    setIsOpen(false);
    onNavigateToSkills?.();
  }, [onNavigateToSkills]);

  // 点击技能项：已选则移除，未选则追加；保持下拉开启，便于多选
  const handleToggleSkill = useCallback((skillName: string) => {
    const sid = useChatStore.getState().activeSessionId;
    if (!sid) return;
    const store = useSessionStore.getState();
    if (selectedSkills.includes(skillName)) {
      store.removeSelectedSkill(sid, skillName);
      onRemoveSkill?.(skillName);
    } else {
      store.addSelectedSkill(sid, skillName);
      onInsertSkill?.(skillName);
    }
  }, [selectedSkills, onInsertSkill, onRemoveSkill]);

  return (
    <div
      ref={menuRef}
      className={clsx('chat-skill-select', isOpen && 'chat-skill-select--open')}
      data-testid="chat-panel-skill-select-root"
    >
      <button
        type="button"
        className="chat-skill-select__trigger"
        onClick={() => setIsOpen((open) => !open)}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        title={t('chat.skillsToggle')}
        data-testid="chat-panel-skill-select-trigger"
      >
        <span className="chat-mode-select__value">
          <span className="chat-mode-select__icon" aria-hidden="true">
            <span className="chat-config-icon chat-config-icon--skill" />
          </span>
          <span className="chat-mode-select__label">{t('chat.skills')}</span>
        </span>
        <svg className="chat-mode-select__chevron" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
        </svg>
      </button>

      {isOpen && (
        <div className="chat-skill-select__menu" role="menu" data-testid="chat-panel-skill-select-menu">
          {/* 顶部搜索框 */}
          <div className="chat-skill-select__search" data-testid="chat-panel-skill-select-search">
            <svg className="chat-skill-select__search-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 3.5a5.5 5.5 0 100 11 5.5 5.5 0 000-11zM17.5 17.5l-3.7-3.7" />
            </svg>
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder={t('chat.skillsSearchPlaceholder')}
              className="chat-skill-select__search-input"
              data-testid="chat-panel-skill-select-search-input"
            />
          </div>

          {loading && (
            <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="loading">{t('skills.detailLoading')}</div>
          )}
          {!loading && errorMessage && (
            <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="error">{errorMessage}</div>
          )}
          {!loading && !errorMessage && installedSkills.length === 0 && (
            <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="no-installed">{t('chat.noInstalledSkills')}</div>
          )}
          {!loading && !errorMessage && installedSkills.length > 0 && filteredSkills.length === 0 && (
            <div className="chat-skill-select__state" data-testid="chat-panel-skill-select-state" data-variant="no-matches">{t('skills.noMatches')}</div>
          )}
          {!loading && !errorMessage && filteredSkills.length > 0 && (
            <>
              <div className="chat-skill-select__list" data-testid="chat-panel-skill-select-list">
                {filteredSkills.map((skill) => {
                  const avatar = getSkillAvatar(skill.name);
                  const isSelected = selectedSkills.includes(skill.name);
                  return (
                    <button
                      type="button"
                      key={skill.name}
                      onClick={() => handleToggleSkill(skill.name)}
                      className={clsx(
                        'chat-skill-select__item',
                        isSelected && 'chat-skill-select__item--selected',
                      )}
                      aria-pressed={isSelected}
                      data-testid="chat-panel-skill-select-item"
                      data-variant={skill.name}
                      title={isSelected ? t('chat.skillsRemove') : t('chat.skillsAdd')}
                    >
                      <div className={`chat-skill-select__avatar ${avatar.color}`} data-testid="chat-panel-skill-select-item-avatar">
                        {avatar.firstChar}
                      </div>
                      <div className="chat-skill-select__item-main" data-testid="chat-panel-skill-select-item-main">
                        <div className="chat-skill-select__item-name" data-testid="chat-panel-skill-select-item-name">{skill.display_name || skill.name}</div>
                        <div className="chat-skill-select__item-desc" data-testid="chat-panel-skill-select-item-desc">
                          {skill.description || t('skills.noDescription')}
                        </div>
                      </div>
                      {isSelected && (
                        <svg className="chat-skill-select__item-check" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={2.2} aria-hidden="true">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M5 10.5l3 3L15 6.5" />
                        </svg>
                      )}
                    </button>
                  );
                })}
              </div>
            </>
          )}

          {/* 底部「技能管理」入口 */}
          <div className="chat-skill-select__footer" data-testid="chat-panel-skill-select-footer">
            <button
              type="button"
              onClick={handleOpenSkillsPage}
              className="chat-skill-select__manage-btn"
              data-testid="chat-panel-skill-select-manage"
            >
              <span className="chat-config-icon chat-config-icon--settings chat-skill-select__manage-icon" aria-hidden="true" />
              <span>{t('chat.skillsManage')}</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function cx(...classes: (string | boolean | undefined | null)[]) {
  return classes.filter(Boolean).join(' ');
}
