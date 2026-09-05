/**
 * SkillPanel 组件
 *
 * Skills 管理面板
 */
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from 'react-i18next';
import { Loader2, Music2 } from 'lucide-react';
import MoreIcon from '../../assets/work-mode/more-rimless.svg?react';
import NewConversationIcon from '../../assets/new_conversation.svg?react';
import UpImgIcon from '../../assets/upImg.svg?react';
import TipIcon from '../../assets/tip.svg?react';
import UpFileIcon from '../../assets/upFile.svg?react';
import LinkIcon from '../../assets/link.svg?react';
import { CategoryTabs, PageHeader, PageToolbarSearch } from '../ui';
import { webRequest } from "../../services/webClient";
import { SourceManagerModal } from "../../features/SourceManagerModal";
import { SkillNetSearchModal } from "../../features/SkillNetSearchModal";
import { ClawHubSearchModal } from "../../features/ClawHubSearchModal";
import { TeamSkillsHubModal } from "../../features/TeamSkillsHubModal";
import { parseConfigBoolean } from "../../features/settings/services/settingsContract";
import { normalizeSkillNetUrl } from "../../utils/skillNetUrl";
import { getSkillAvatar } from "../../utils/skillAvatar";
import { computeMySkills, filterEnabledMySkills } from "../../utils/mySkills";
import { buildSkillVersionOptions } from "./skillVersionOptions";
import {
  getStoredOAuthToken,
  getStoredOAuthProvider,
  buildOAuthUrl,
  type OAuthProvider,
} from "../../utils/gitcodeOAuth";
import { SkillGraphPanel, type SkillGraphPanelHandle } from "../SkillGraphPanel";
import { MarkdownRenderer } from "../MarkdownRenderer";
import { Switch } from "../Switch";
import { coordinateSymphonyEnabledChange } from "./symphonyGraphAction";
import {
  canBuildSkillRetrievalIndex,
  parseSkillRetrievalStatus,
} from "./skillRetrievalStatus";

/** 刷新会 git pull marketplace，略放宽；普通进页单次 RPC 一般很快。 */
const SKILLS_FETCH_TIMEOUT_REFRESH_MS = 60_000;
const SKILLS_FETCH_TIMEOUT_NORMAL_MS = 30_000;
const GRAPH_READING_MIN_VISIBLE_MS = 500;

const CARD_CLASS = "page-card h-40 group relative text-left border border-border bg-panel hover:bg-card cursor-pointer rounded-[8px] pt-6 pb-4 px-4 flex flex-col overflow-visible";

// ── 发布表单校验（与 skillhub 对齐） ──
// 技能名：小写字母开头，小写字母/数字/连字符，最长 64
const SKILL_NAME_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const SKILL_NAME_MAX_LEN = 64;
// 版本号：严格 x.y.z 三段数字
const VERSION_PATTERN = /^[0-9]+\.[0-9]+\.[0-9]+$/;
// 显示名：非空，最长 128
const DISPLAY_NAME_MAX_LEN = 128;

const PREVIEWABLE_MIME_TYPES = ['application/json', 'application/xml', 'application/javascript', 'application/x-yaml'];
const PREVIEWABLE_FILE_EXTS = ['md', 'mdx', 'json', 'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp'];

interface FileTreeNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size: number | null;
  mime_type: string | null;
  children: FileTreeNode[];
}

/** 判断文件是否可预览（纯函数，供文件树使用） */
function isFilePreviewable(entry: { type: string; mime_type: string | null; name: string }): boolean {
  if (entry.type !== 'file') return false;
  const mime = entry.mime_type || '';
  if (mime.startsWith('text/')) return true;
  if (mime.startsWith('image/')) return true;
  if (PREVIEWABLE_MIME_TYPES.includes(mime)) return true;
  const ext = entry.name.split('.').pop()?.toLowerCase() || '';
  return PREVIEWABLE_FILE_EXTS.includes(ext);
}

type SkillItem = {
  name: string;
  /** 展示名（保留安装来源的原始大小写，如 ClawHub 的 Weather）；缺省回退到 name */
  display_name?: string;
  description: string;
  source: string;
  version: string;
  author: string;
  tags: string[];
  allowed_tools: string[];
  marketplace?: string;
  /** SkillNet 等安装来源 URL，与在线搜索 skill_url 对照「已安装」 */
  origin?: string;
  /** 是否为内置技能（不允许删除） */
  is_builtin?: boolean;
  /** 是否为内置技能的来源（源码中存在内置版本） */
  is_builtin_source?: boolean;
  /** 本地技能目录是否存在 evolutions.json */
  has_evolutions?: boolean;
  /** 是否启用 */
  enabled?: boolean;
  /** 是否已安装 */
  installed?: boolean;
  /** 技能文件路径（列表去重 / React key） */
  path?: string;
  /** 技能类型：skill | swarm_skill | multimodal_skill */
  skill_type?: string;
  /** 是否已发布到 SkillHub */
  published?: boolean;
};

type InstalledPluginItem = {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit?: string | null;
  skills: (string | { name: string; version?: string | null })[];
};

type SkillDetail = SkillItem & {
  content: string;
  file_path: string;
};

type HubSkillDetailData = {
  short_desc?: string | null;
  detail_desc?: string | null;
};

type HubSkillDetail = {
  success: boolean;
  asset_id: string;
  version: string;
  data?: HubSkillDetailData | null;
};

type SkillVersion = {
  version: string;
  is_default: boolean;
  source: string;
  available: boolean;
  created_at: string;
  updated_at: string;
};

type SkillVersionsListResponse = {
  success: boolean;
  name: string;
  default_version: string | null;
  versions: SkillVersion[];
};

type SkillFileEntry = {
  path: string;
  type: 'file' | 'directory';
  size: number | null;
  mime_type: string | null;
};

type SkillFilesListResponse = {
  name: string;
  files: SkillFileEntry[];
};

type SkillFilePreview = {
  name: string;
  path: string;
  type: 'file';
  mime_type: string;
  size: number;
  encoding?: string;
  content?: string;
  download_url?: string;
};

type SkillRebuildResponse = {
  success: boolean;
  result_type: 'followup';
  action: string;
  followup_prompt: string;
  skill_name: string;
  rebuild_target: {
    version: string | null;
    is_default: boolean;
    skill_dir: string;
    content_root: string | null;
    swap_workspace: boolean;
  };
};

type EvolutionChange = {
  section?: string;
  action?: string;
  content: string;
  target?: string;
};

type EvolutionEntry = {
  id: string;
  source?: string;
  timestamp?: string;
  context?: string;
  change: EvolutionChange;
  applied?: boolean;
};

type EvolutionGetResponse = {
  exists: boolean;
  valid?: boolean;
  detail?: string;
  entries?: EvolutionEntry[];
};

type LoadState = "idle" | "loading" | "success" | "error";

interface SkillPanelProps {
  sessionId: string;
  isConnected: boolean;
  symphonyEnabled: boolean;
  onSymphonyEnabledChange: (enabled: boolean) => Promise<boolean>;
  onNavigateToSettings?: () => void;
  /** 当前是否处于激活状态（左边栏选中技能） */
  isActive?: boolean;
}

/** 与后端一致：tags/allowed_tools 可能是逗号分隔字符串，统一为 string[] */
function coerceStringList(val: unknown): string[] {
  if (val == null) return [];
  if (Array.isArray(val)) {
    return val.map((x) => String(x).trim()).filter(Boolean);
  }
  if (typeof val === "string") {
    const s = val.trim();
    if (!s) return [];
    return s.includes(",")
      ? s.split(",").map((p) => p.trim()).filter(Boolean)
      : [s];
  }
  return [String(val)];
}

function normalizeSkillItem<T extends SkillItem>(raw: T): T {
  return {
    ...raw,
    tags: coerceStringList(raw.tags),
    allowed_tools: coerceStringList(raw.allowed_tools),
  };
}

interface MarketplacePluginItem {
  asset_id: string;
  name: string;
  display_name?: string | null;
  short_desc?: string | null;
  detail_desc?: string | null;
  icon_uri?: string | null;
  publisher_name: string;
  tags?: string[] | null;
  plugin_type?: string | null;
  category_id?: string | null;
  category_name?: string | null;
  latest_version?: string | null;
  install_count: number;
  like_count: number;
  view_count: number;
  moderation_status?: string | null;
  // 搜索结果额外字段
  source?: string | null;
  identifier?: string | null;
  owner_handle?: string | null;
  native_score?: number | null;
  category?: string | null;
  updated_at?: number | null;
  exact_match?: boolean;
}

const MARKETPLACE_CATEGORIES = ["all", "software-development", "office-productivity", "content-creation", "multimodal-media", "data-science-research", "compliance-legal", "lifestyle-health", "finance-wealth"] as const;

/**
 * 将技能内容中的图片路径转换为 /file-api/raw-file 可访问的 URL。
 * 处理两种情况：
 * 1. 相对路径（references/img_00.png）→ 直接拼接技能目录
 * 2. 后端改写的 /file-api/download?token=xxx → 解码 token 获取 relative_path
 * skillFilePath 为 SKILL.md 的绝对路径，用于推导技能所在目录。
 */
function transformSkillContentImages(content: string, skillFilePath: string): string {
  if (!content || !skillFilePath) return content;
  const lastSlash = Math.max(skillFilePath.lastIndexOf('/'), skillFilePath.lastIndexOf('\\'));
  const skillDir = lastSlash >= 0 ? skillFilePath.substring(0, lastSlash).replace(/\\/g, '/') : '';

  const toRawFileUrl = (relativePath: string): string => {
    const fullPath = skillDir ? `${skillDir}/${relativePath}`.replace(/\\/g, '/') : relativePath;
    return `/file-api/raw-file?path=${encodeURIComponent(fullPath)}`;
  };

  return content.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (match, alt, imgPath: string) => {
    // 跳过外部 URL 和 data URI
    if (/^(https?:|data:|file:\/\/\/)/.test(imgPath)) return match;

    // 处理后端改写的 /file-api/download?token=xxx URL
    if (imgPath.startsWith('/file-api/download?token=')) {
      try {
        const token = new URL(imgPath, 'http://localhost').searchParams.get('token') || '';
        const payloadB64 = token.split('.')[0];
        const payload = JSON.parse(atob(payloadB64));
        if (payload.relative_path) {
          return `![${alt}](${toRawFileUrl(payload.relative_path)})`;
        }
      } catch {
        // 解码失败，保留原 URL
      }
      return match;
    }

    // 处理相对路径
    if (imgPath.startsWith('/file-api/')) return match;
    return `![${alt}](${toRawFileUrl(imgPath)})`;
  });
}

/** 表单字段问号 tooltip（position: fixed，不受 overflow 裁剪） */
function FormFieldTooltip({ text }: { text: string }) {
  const [show, setShow] = useState(false);
  const [pos, setPos] = useState<{ bottom: number; left: number }>({ bottom: 0, left: 0 });
  const ref = useRef<HTMLSpanElement>(null);

  const handleEnter = useCallback(() => {
    if (ref.current) {
      const rect = ref.current.getBoundingClientRect();
      setPos({ bottom: window.innerHeight - rect.top + 6, left: rect.left + rect.width / 2 });
    }
    setShow(true);
  }, []);

  const handleLeave = useCallback(() => setShow(false), []);

  return (
    <>
      <span
        ref={ref}
        className="ml-1 inline-flex items-center cursor-help"
        onMouseEnter={handleEnter}
        onMouseLeave={handleLeave}
      >
        <svg className="w-3.5 h-3.5 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.171 1.025 1.171 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.453.73-1.453 1.577v.001m.001 2.174v.01" />
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 21a.75.75 0 000-1.5.75.75 0 000 1.5z" />
          <circle cx="12" cy="12" r="9" />
        </svg>
      </span>
      {show && createPortal(
        <div
          className="fixed z-[10000] px-2.5 py-1.5 text-xs text-text bg-[var(--color-surface-popover)] border border-border rounded shadow-lg"
          style={{
            bottom: pos.bottom,
            left: pos.left,
            transform: 'translateX(-50%)',
            width: 'max-content',
            maxWidth: '300px',
            whiteSpace: 'normal',
            lineHeight: '1.4',
          }}
        >
          {text}
        </div>,
        document.body
      )}
    </>
  );
}

/** 按钮上方固定定位提示条（合成新版本 / 去试试 / 链接说明 共用） */
function TopAnchorTooltip({ pos, text }: { pos: { left: number; top: number }; text: string }) {
  return (
    <div
      className="fixed whitespace-nowrap rounded-[8px] h-[50px] flex items-center px-2.5 text-xs text-text bg-[var(--color-surface-popover)] border border-border shadow-lg pointer-events-none z-[9999]"
      style={{
        left: pos.left,
        top: pos.top - 50 - 6,
        transform: 'translateX(-50%)',
      }}
    >
      {text}
    </div>
  );
}

/** 弹窗右上角关闭按钮 */
function ModalCloseButton({ onClick, label, testId }: { onClick: () => void; label: string; testId?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      data-testid={testId}
      className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text-muted hover:text-text"
    >
      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
      </svg>
    </button>
  );
}

/** 技能类型徽标（团队技能 / 多模态） */
function SkillTypeBadge({ skillType, withTruncate = false }: { skillType?: string; withTruncate?: boolean }) {
  const { t } = useTranslation();
  if (skillType === 'swarm_skill') {
    return (
      <span className={`px-2 h-5 inline-flex items-center rounded bg-accent/10 border border-border text-xs text-text-link${withTruncate ? ' truncate' : ''}`}>
        {t('skills.skillTypes.team')}
      </span>
    );
  }
  if (skillType === 'multimodal_skill') {
    return (
      <span className={`px-2 h-5 inline-flex items-center rounded border border-border text-xs bg-[var(--color-skill-badge-multimodal-surface)] text-[var(--color-skill-badge-multimodal-text)]${withTruncate ? ' truncate' : ''}`}>
        {t('skills.skillTypes.multimodal')}
      </span>
    );
  }
  return null;
}

/** 技能广场卡片（团队专页 / 搜索结果 / 精选团队 / 精选技能 共用），右侧操作按钮由调用方传入 */
function HubSkillCard({ skill, onSelect, action }: {
  skill: MarketplacePluginItem;
  onSelect: () => void;
  action: ReactNode;
}) {
  const { t } = useTranslation();
  const avatar = getSkillAvatar(skill.name);
  const displayName = skill.display_name || skill.name;
  return (
    <div onClick={onSelect} className={CARD_CLASS} data-testid="skill-card" data-variant={skill.asset_id}>
      <div className="flex items-center gap-3 flex-shrink-0">
        <div className={`w-12 h-12 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
          {avatar.firstChar}
        </div>
        <div className="min-w-0 flex-1 h-full flex flex-col justify-between">
          <div className="flex items-center gap-1.5">
            <span className="text-sm font-semibold text-text-strong truncate leading-5" data-testid="skill-card-name">
              {displayName}
            </span>
          </div>
          {skill.tags && skill.tags.length > 0 && (
            <div className="mt-1 flex items-center gap-1.5 flex-wrap">
              {skill.tags.map(tag => (
                <span
                  key={tag}
                  className="h-5 px-2 inline-flex items-center rounded text-xs bg-[var(--color-tag-neutral-surface)]"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="shrink-0">
          {action}
        </div>
      </div>
      <div className="text-xs text-text-muted mt-[18px] line-clamp-2 leading-[22px]">
        {skill.short_desc || skill.detail_desc || t('skills.noDescription')}
      </div>
    </div>
  );
}

/** 通用筛选下拉（发布状态 / 启用状态 共用） */
function FilterDropdown<T extends string>({ open, onToggle, onClose, value, onChange, options, style }: {
  open: boolean;
  onToggle: (open: boolean) => void;
  onClose: () => void;
  value: T;
  onChange: (value: T) => void;
  options: { value: T; label: string }[];
  style?: CSSProperties;
}) {
  const selected = options.find(o => o.value === value);
  return (
    <div className="relative" style={style}>
      <button
        type="button"
        onClick={() => onToggle(!open)}
        className="flex items-center justify-between w-full h-[32px] text-xs text-text bg-transparent"
      >
        <span className="truncate">{selected ? selected.label : ''}</span>
        <svg className={`shrink-0 w-3.5 h-3.5 transition-transform ${open ? 'rotate-180' : ''} text-text-muted`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={onClose} />
          <div className="absolute right-0 top-full mt-1 z-50 min-w-[120px] rounded-lg border border-border bg-panel shadow-lg py-1">
            {options.map(opt => (
              <button
                key={opt.value}
                type="button"
                onClick={() => onChange(opt.value)}
                className={`flex items-center w-full px-3 py-2 text-sm text-left hover:bg-secondary ${opt.value === value ? 'text-chat-accent' : 'text-text-muted'}`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

export function SkillPanel({
  sessionId,
  isConnected,
  symphonyEnabled,
  onSymphonyEnabledChange,
  onNavigateToSettings,
  isActive = false,
}: SkillPanelProps) {
  const { t, i18n } = useTranslation();
  const [activeTab, setActiveTab] = useState<"my" | "marketplace" | "graph">("marketplace");
  const [mySkillsSubTab, setMySkillsSubTab] = useState<"all" | "enabled" | "disabled">("enabled");
  const [mySkillsPublishFilter, setMySkillsPublishFilter] = useState<"all" | "published" | "unpublished">("all");
  const [marketplaceCategory, setMarketplaceCategory] = useState<"all" | "software-development" | "office-productivity" | "content-creation" | "multimodal-media" | "data-science-research" | "compliance-legal" | "lifestyle-health" | "finance-wealth">("all");
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [plugins, setPlugins] = useState<InstalledPluginItem[]>([]);
  const [hubSkills, setHubSkills] = useState<MarketplacePluginItem[]>([]);
  const [hubLoading, setHubLoading] = useState(false);
  const [search, setSearch] = useState("");
  const prevIsActiveRef = useRef(isActive);
  const mountedRef = useRef(false);
  const [selectedSkill, setSelectedSkill] = useState<SkillDetail | null>(null);
  const [listState, setListState] = useState<LoadState>("idle");
  const [detailState, setDetailState] = useState<LoadState>("idle");
  const [actionTarget, setActionTarget] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [messageType, setMessageType] = useState<"success" | "error" | "loading" | null>(null);
  const messageTimerRef = useRef<number | null>(null);
  const skillGraphPanelRef = useRef<SkillGraphPanelHandle | null>(null);
  const graphReadingStartedAtRef = useRef<number | null>(null);
  const graphReadingTimerRef = useRef<number | null>(null);
  const evolutionSaveTimerRef = useRef<number | null>(null);
  /** 技能广场请求序号：防抖搜索/分类切换时丢弃过期响应 */
  const hubFetchSeqRef = useRef(0);
  const [graphReading, setGraphReading] = useState(false);
  const [symphonyEnabledDraft, setSymphonyEnabledDraft] = useState(symphonyEnabled);
  const [symphonySaving, setSymphonySaving] = useState(false);
  const [symphonySaveError, setSymphonySaveError] = useState<string | null>(null);
  const [graphActionError, setGraphActionError] = useState<string | null>(null);
  const [indexRecommendationVisible, setIndexRecommendationVisible] = useState(false);
  const [indexRecommendationBuilding, setIndexRecommendationBuilding] = useState(false);
  const indexRecommendationRequestRef = useRef(0);
  const [knowledgeTaskCount, setKnowledgeTaskCount] = useState(0);
  const [openMenuSkillName, setOpenMenuSkillName] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      if (messageTimerRef.current !== null) {
        window.clearTimeout(messageTimerRef.current);
      }
      if (graphReadingTimerRef.current !== null) {
        window.clearTimeout(graphReadingTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!symphonySaving) {
      setSymphonyEnabledDraft(symphonyEnabled);
    }
  }, [symphonyEnabled, symphonySaving]);

  const clearGraphActionError = useCallback(() => {
    setGraphActionError(null);
  }, []);

  const updateSymphonyEnabled = useCallback(async (enabled: boolean) => {
    if (!isConnected || symphonySaving || enabled === symphonyEnabledDraft) return;
    setSymphonyEnabledDraft(enabled);
    setSymphonySaving(true);
    setSymphonySaveError(null);
    const result = await coordinateSymphonyEnabledChange({
      enabled,
      save: onSymphonyEnabledChange,
      getGraphPanel: () => skillGraphPanelRef.current,
      request: webRequest,
      refreshFailedMessage: t('skills.graph.errors.refreshFailed'),
      cancelFailedMessage: t('skills.graph.errors.cancelFailed'),
      onGraphActionStart: clearGraphActionError,
    });
    if (result.configSaveFailed) {
      setSymphonyEnabledDraft(symphonyEnabled);
      setSymphonySaveError(t('skills.graph.orchestration.saveFailed'));
      setSymphonySaving(false);
      return;
    }
    if (!result.appliedWithoutRestart) {
      setSymphonySaving(false);
      return;
    }
    if (result.graphActionError) {
      setGraphActionError(result.graphActionError);
    }
    setSymphonySaving(false);
  }, [clearGraphActionError, isConnected, onSymphonyEnabledChange, symphonyEnabled, symphonyEnabledDraft, symphonySaving, t]);

  const updateGraphReading = useCallback((reading: boolean) => {
    if (graphReadingTimerRef.current !== null) {
      window.clearTimeout(graphReadingTimerRef.current);
      graphReadingTimerRef.current = null;
    }
    if (reading) {
      graphReadingStartedAtRef.current = Date.now();
      setGraphReading(true);
      return;
    }
    const startedAt = graphReadingStartedAtRef.current;
    graphReadingStartedAtRef.current = null;
    const elapsed = startedAt == null ? GRAPH_READING_MIN_VISIBLE_MS : Date.now() - startedAt;
    const delay = Math.max(0, GRAPH_READING_MIN_VISIBLE_MS - elapsed);
    if (delay === 0) {
      setGraphReading(false);
      return;
    }
    graphReadingTimerRef.current = window.setTimeout(() => {
      graphReadingTimerRef.current = null;
      setGraphReading(false);
    }, delay);
  }, []);

  const showMessage = useCallback((type: "success" | "error" | "loading", text: string) => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
      messageTimerRef.current = null;
    }
    const displayText = type === "success" ? `√ ${text}` : text;
    setMessage(displayText);
    setMessageType(type);
    // loading 持续到下一次消息；错误信息显示时间更长（8秒）
    if (type === "loading") {
      return;
    }
    const duration = type === "error" ? 8000 : 3000;
    messageTimerRef.current = window.setTimeout(() => {
      setMessage(null);
      setMessageType(null);
      messageTimerRef.current = null;
    }, duration);
  }, []);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const [uploadSkillModalOpen, setUploadSkillModalOpen] = useState(false);
  const [docToSkillModalOpen, setDocToSkillModalOpen] = useState(false);
  const [uploadSkillPath, setUploadSkillPath] = useState("");
  const [uploadSkillFile, setUploadSkillFile] = useState<File | null>(null);
  const [docToSkillPath, setDocToSkillPath] = useState("");
  const [docToSkillFile, setDocToSkillFile] = useState<File | null>(null);
  const [docToSkillSource, setDocToSkillSource] = useState<"local" | "link">("local");
  const [docToSkillLink, setDocToSkillLink] = useState("");
  const [docToSkillDesc, setDocToSkillDesc] = useState("");
  const [docToSkillTooltip, setDocToSkillTooltip] = useState<{ left: number; top: number } | null>(null);
  const [publishFilterOpen, setPublishFilterOpen] = useState(false);
  const [enableFilterOpen, setEnableFilterOpen] = useState(false);
  const [skillNetModalOpen, setSkillNetModalOpen] = useState(false);
  const [clawHubModalOpen, setClawHubModalOpen] = useState(false);
  const [teamSkillsHubModalOpen, setTeamSkillsHubModalOpen] = useState(false);
  const [synthesizeTooltip, setSynthesizeTooltip] = useState<{ left: number; top: number } | null>(null);
  const [goTryTooltip, setGoTryTooltip] = useState<{ left: number; top: number } | null>(null);
  const [detailMenuOpen, setDetailMenuOpen] = useState(false);
  const [publishDrawerOpen, setPublishDrawerOpen] = useState(false);
  const [publishSkillName, setPublishSkillName] = useState("");
  const [publishVersion, setPublishVersion] = useState("");
  const [publishDisplayName, setPublishDisplayName] = useState("");
  const [publishNoticeVisible, setPublishNoticeVisible] = useState(true);
  const [oauthLoginOpen, setOauthLoginOpen] = useState(false);
  const [oauthError, setOauthError] = useState<string | null>(null);
  const [oauthLoadingProvider, setOauthLoadingProvider] = useState<OAuthProvider | null>(null);
  const [selectedHubSkill, setSelectedHubSkill] = useState<MarketplacePluginItem | null>(null);
  const [hubDetail, setHubDetail] = useState<HubSkillDetail | null>(null);
  const [hubDetailState, setHubDetailState] = useState<LoadState>("idle");
  const [marketplaceSubView, setMarketplaceSubView] = useState<'list' | 'team' | 'detail'>('list');
  const [publishVersionDesc, setPublishVersionDesc] = useState("");
  const [publishForce, setPublishForce] = useState(false);
  const [publishFieldErrors, setPublishFieldErrors] = useState<Record<string, string>>({});
  const [publishError, setPublishError] = useState<string | null>(null);
  const [detailTab, setDetailTab] = useState<"content" | "files" | "experience">("content");
  const [evolutionEntries, setEvolutionEntries] = useState<EvolutionEntry[]>([]);
  const [evolutionListState, setEvolutionListState] = useState<LoadState>("idle");
  const [evolutionMessage, setEvolutionMessage] = useState<string | null>(null);
  const [evolutionMessageType, setEvolutionMessageType] = useState<"success" | "error" | null>(null);
  const [evolutionFormatError, setEvolutionFormatError] = useState<string | null>(null);
  const [skillVersions, setSkillVersions] = useState<SkillVersion[]>([]);
  const [skillVersionsDefault, setSkillVersionsDefault] = useState<string | null>(null);
  const [versionsLoadState, setVersionsLoadState] = useState<LoadState>("idle");
  const [skillFiles, setSkillFiles] = useState<SkillFileEntry[]>([]);
  const [filesLoadState, setFilesLoadState] = useState<LoadState>("idle");
  const [filePreview, setFilePreview] = useState<SkillFilePreview | null>(null);
  const [filePreviewPath, setFilePreviewPath] = useState<string | null>(null);
  const [expandedFileFolders, setExpandedFileFolders] = useState<Set<string>>(new Set([""]));
  const [rebuildLoading, setRebuildLoading] = useState(false);
  const withSession = useCallback(
    <T extends Record<string, unknown> = Record<string, unknown>>(params?: T): T & { session_id: string } => ({
      ...(params || {} as T),
      session_id: sessionId,
    }),
    [sessionId]
  );

  const installedSkillMap = useMemo(() => {
    const map = new Map<string, InstalledPluginItem>();
    plugins.forEach((plugin) => {
      plugin.skills.forEach((skill) => {
        const skillName = typeof skill === 'string' ? skill : skill.name;
        if (!map.has(skillName)) {
          map.set(skillName, plugin);
        }
      });
    });
    return map;
  }, [plugins]);

  const installedSkillNames = useMemo(
    () => new Set(installedSkillMap.keys()),
    [installedSkillMap]
  );

  /** 已安装技能的来源 URL（规范化），与 SkillNet 搜索结果的 skill_url 匹配 */
  const installedSkillOrigins = useMemo(() => {
    const set = new Set<string>();
    for (const s of skills) {
      const o = s.origin?.trim();
      if (o) {
        set.add(normalizeSkillNetUrl(o));
      }
    }
    return set;
  }, [skills]);

  const filteredSkills = useMemo(() => {
    let result = skills;
    if (activeTab === "my") {
      // 2026-08-21：抽成 utils/mySkills.ts 的 computeMySkills，跟"手动创建插件"的"添加技能"
      // 弹窗共用同一份"我的技能"判定规则，见该文件头注释。这里原本是"候选集过滤+排除内置未装"
      // 两步（第二步挪到了下面 visibleSkills 里），computeMySkills 已经把两步合并，语义不变
      // （排除条件不依赖搜索关键字，跟下面的关键字过滤谁先谁后结果一样）。
      result = computeMySkills(result, installedSkillNames);
    }
    const keyword = search.trim().toLowerCase();
    if (!keyword) return result;
    return result.filter((skill) => {
      const haystack = [
        skill.name,
        skill.display_name,
        skill.description,
        skill.author,
        coerceStringList(skill.tags).join(" "),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(keyword);
    });
  }, [skills, search, activeTab, installedSkillNames]);

  const visibleSkills = useMemo(() => {
    return [...filteredSkills].sort((a, b) => {
      const aSkillNet = a.source === "skillnet" ? 1 : 0;
      const bSkillNet = b.source === "skillnet" ? 1 : 0;
      if (aSkillNet !== bSkillNet) {
        return bSkillNet - aSkillNet;
      }
      return a.name.localeCompare(b.name);
    });
  }, [filteredSkills]);

  const fetchHubSkills = useCallback(async (category: string) => {
    const seq = ++hubFetchSeqRef.current;
    setHubLoading(true);
    try {
      // 后端 PR#5336: skills.swarmskillshub.recommend → POST /api/v1/recommend
      // 支持 category_id、plugin_type、top_k、market_url 参数
      const params = withSession({
        top_k: 50,
        market_url: 'http://119.8.233.112:8080',
        ...(category !== 'all' ? { category_id: category } : {}),
      });
      const data = await webRequest<{
        success: boolean;
        skills?: Array<{
          asset_id: string;
          name: string;
          display_name?: string;
          summary?: string;
          version?: string;
          plugin_type?: string;
          tags?: string[];
        }>;
        detail?: string;
      }>("skills.swarmskillshub.recommend", params, { timeoutMs: 30000 });

      if (!data.success) throw new Error(data.detail || 'Recommend failed');
      if (seq !== hubFetchSeqRef.current) return;
      const items: MarketplacePluginItem[] = (data.skills || []).map(s => ({
        asset_id: s.asset_id,
        name: s.name,
        display_name: s.display_name || s.name,
        short_desc: s.summary || '',
        publisher_name: '',
        install_count: 0,
        like_count: 0,
        view_count: 0,
        plugin_type: s.plugin_type || null,
        tags: s.tags || null,
        latest_version: s.version || null,
      }));
      setHubSkills(items);
    } catch (error) {
      console.error('Failed to fetch SkillHub recommend:', error);
      if (seq !== hubFetchSeqRef.current) return;
      setHubSkills([]);
    } finally {
      if (seq === hubFetchSeqRef.current) setHubLoading(false);
    }
  }, [withSession]);

  const fetchOnlineSearch = useCallback(async (query: string) => {
    const seq = ++hubFetchSeqRef.current;
    setHubLoading(true);
    try {
      // 文档 2~4: skills.online_search.search
      const data = await webRequest<{
        success: boolean;
        partial?: boolean;
        items?: Array<{
          source: string;
          identifier: string;
          name: string;
          display_name: string;
          description: string;
          version: string;
          author: string;
          is_team_skill: boolean;
          native_score: number | null;
          category: string;
          updated_at: number;
          source_rank: number;
          fusion_score: number;
          exact_match: boolean;
          matched_source_count: number;
          owner_handle?: string;
        }>;
        sources?: Array<{
          source: string;
          status: 'success' | 'error' | 'skipped';
          count: number;
          detail?: string;
          detail_key?: string;
        }>;
        detail?: string;
      }>("skills.online_search.search", withSession({
        q: query,
        limit: 50,
      }), { timeoutMs: 45000 });

      // success=false: 参数非法或所有来源均失败
      if (!data.success) {
        throw new Error(data.detail || 'Search failed');
      }

      // partial=true: 部分来源失败，仍展示已有结果
      if (data.partial) {
        const failedSources = (data.sources || [])
          .filter(s => s.status === 'error')
          .map(s => s.source);
        if (failedSources.length > 0) {
          console.warn('Partial search: sources failed:', failedSources);
        }
      }

      if (seq !== hubFetchSeqRef.current) return;
      const items: MarketplacePluginItem[] = (data.items || []).map(s => ({
        asset_id: s.identifier,
        name: s.name,
        display_name: s.display_name || s.name,
        short_desc: s.description || '',
        publisher_name: s.author || '',
        install_count: s.native_score ?? 0,
        like_count: 0,
        view_count: 0,
        plugin_type: s.is_team_skill ? 'swarmskill' : 'skill',
        latest_version: s.version || null,
        source: s.source,
        identifier: s.identifier,
        owner_handle: s.owner_handle || null,
        native_score: s.native_score,
        category: s.category || null,
        updated_at: s.updated_at || null,
        exact_match: s.exact_match,
      }));
      setHubSkills(items);
    } catch (error) {
      console.error('Failed to fetch online search:', error);
      if (seq !== hubFetchSeqRef.current) return;
      setHubSkills([]);
    } finally {
      if (seq === hubFetchSeqRef.current) setHubLoading(false);
    }
  }, [withSession]);

  useEffect(() => {
    if (activeTab !== 'marketplace') return;
    const keyword = search.trim();
    const timer = setTimeout(() => {
      if (keyword) {
        fetchOnlineSearch(keyword);
      } else {
        fetchHubSkills(marketplaceCategory);
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [activeTab, marketplaceCategory, search, fetchHubSkills, fetchOnlineSearch]);

  // 按 plugin_type 分组：swarmskill → 精选团队技能，其余 → 精选技能
  const { teamSkills, featuredSkills } = useMemo(() => {
    const team: MarketplacePluginItem[] = [];
    const featured: MarketplacePluginItem[] = [];
    for (const skill of hubSkills) {
      if (skill.plugin_type === 'swarmskill') {
        team.push(skill);
      } else {
        featured.push(skill);
      }
    }
    return { teamSkills: team, featuredSkills: featured };
  }, [hubSkills]);

  const visibleTeamSkills = teamSkills.slice(0, 3);

  const fetchSkills = useCallback(async (refreshMarketplaces = false) => {
    setListState("loading");
    try {
      const data = await webRequest<{
        skills?: SkillItem[];
        plugins?: InstalledPluginItem[];
      }>(
        "skills.list",
        {
          with_installed: true,
          ...(refreshMarketplaces ? { refresh_marketplaces: true } : {}),
        },
        {
          timeoutMs: refreshMarketplaces
            ? SKILLS_FETCH_TIMEOUT_REFRESH_MS
            : SKILLS_FETCH_TIMEOUT_NORMAL_MS,
        }
      );
      setSkills((data.skills || []).map(normalizeSkillItem));
      setPlugins(data.plugins || []);
      setListState("success");
    } catch (error) {
      console.error(error);
      setListState("error");
    }
  }, []);

  const fetchSkillDetail = useCallback(
    async (skillName: string, version?: string) => {
      setDetailState("loading");
      try {
        const data = await webRequest<SkillDetail>(
          "skills.get",
          withSession({ name: skillName, ...(version ? { version } : {}) })
        );
        setSelectedSkill(normalizeSkillItem(data));
        setDetailTab("content");
        setDetailState("success");
        setFilePreview(null);
        setFilePreviewPath(null);
      } catch (error) {
        console.error(error);
        setDetailState("error");
      }
    },
    [withSession]
  );

  const fetchHubSkillDetail = useCallback(
    async (skill: MarketplacePluginItem) => {
      setHubDetailState("loading");
      setMarketplaceSubView('detail');
      try {
        // ClawHub 条目没有 detail RPC，直接用搜索结果中的描述
        if (skill.source === 'clawhub') {
          setHubDetail({
            success: true,
            asset_id: skill.asset_id,
            version: skill.latest_version || '',
            data: {
              short_desc: skill.short_desc,
              detail_desc: skill.short_desc || skill.detail_desc || '',
            },
          });
          setHubDetailState("success");
          return;
        }

        // SwarmSkillHub 条目：通过 asset_id 查询详情
        const data = await webRequest<HubSkillDetail>(
          "skills.swarmskillshub.detail",
          withSession({
            asset_id: skill.asset_id,
          }),
          { timeoutMs: 30000 }
        );
        setHubDetail(data);
        setHubDetailState("success");
      } catch (error) {
        console.error(error);
        setHubDetailState("error");
      }
    },
    [withSession]
  );

  const handleInstallHubSkill = useCallback(
    async (skill: MarketplacePluginItem) => {
      const installKey = skill.identifier || skill.asset_id;
      setActionTarget(`install:${installKey}`);
      try {
        const isTeamSkillHub = !skill.source || skill.source === 'teamskillshub';
        let data: { success: boolean; skill?: { name: string }; detail?: string; message?: string };

        if (isTeamSkillHub) {
          // SwarmSkillHub 安装：通过 asset_id
          data = await webRequest<{
            success: boolean;
            skill?: { name: string };
            detail?: string;
          }>(
            "skills.teamskillshub.install",
            withSession({ asset_id: skill.asset_id, force: false }),
            { timeoutMs: 60000 }
          );
        } else {
          // ClawHub 安装：通过 spec（slug@clawhub）
          const spec = `${skill.name}@clawhub`;
          data = await webRequest<{
            success: boolean;
            detail?: string;
            message?: string;
          }>(
            "skills.install",
            withSession({ spec, force: false }),
            { timeoutMs: 60000 }
          );
        }

        if (!data.success) {
          throw new Error(data.detail || data.message || t('skills.errors.installFailed'));
        }
        showMessage("success", t('skills.messages.installed', { name: data.skill?.name || skill.name }));
        await fetchSkills();
      } catch (error) {
        console.error(error);
        const errorMessage = error instanceof Error ? error.message : String(error);
        showMessage("error", errorMessage || t('skills.errors.installFailedHint'));
      } finally {
        setActionTarget(null);
      }
    },
    [withSession, fetchSkills, t]
  );

  const fetchSkillVersions = useCallback(
    async (skillName: string) => {
      setVersionsLoadState("loading");
      try {
        const data = await webRequest<SkillVersionsListResponse>(
          "skills.versions.list",
          withSession({ name: skillName })
        );
        setSkillVersions(data.versions || []);
        setSkillVersionsDefault(data.default_version);
        setVersionsLoadState("success");
      } catch (error) {
        console.error(error);
        setVersionsLoadState("error");
      }
    },
    [withSession]
  );

  const fetchSkillFiles = useCallback(
    async (skillName: string) => {
      setFilesLoadState("loading");
      try {
        const data = await webRequest<SkillFilesListResponse>(
          "skills.files.list",
          withSession({ name: skillName })
        );
        setSkillFiles(data.files || []);
        setFilesLoadState("success");
      } catch (error) {
        console.error(error);
        setFilesLoadState("error");
      }
    },
    [withSession]
  );

  const fetchFilePreview = useCallback(
    async (skillName: string, filePath: string) => {
      try {
        const data = await webRequest<SkillFilePreview>(
          "skills.files.get",
          withSession({ name: skillName, path: filePath })
        );
        setFilePreview(data);
        setFilePreviewPath(filePath);
      } catch (error) {
        console.error(error);
        setFilePreview(null);
      }
    },
    [withSession]
  );

  // ---- 文件树构建 ----
  const skillFileTree = useMemo(() => {
    const root: FileTreeNode = { name: '', path: '', type: 'directory', size: null, mime_type: null, children: [] };
    const dirMap = new Map<string, FileTreeNode>();
    dirMap.set('', root);
    for (const entry of skillFiles) {
      const parts = entry.path.split('/').filter(Boolean);
      let currentPath = '';
      let currentNode = root;
      for (let i = 0; i < parts.length; i++) {
        const part = parts[i];
        const isLast = i === parts.length - 1;
        currentPath = currentPath ? `${currentPath}/${part}` : part;
        if (isLast && entry.type === 'file') {
          currentNode.children.push({ name: part, path: entry.path, type: 'file', size: entry.size, mime_type: entry.mime_type, children: [] });
        } else {
          if (!dirMap.has(currentPath)) {
            const dirNode: FileTreeNode = { name: part, path: currentPath, type: 'directory', size: null, mime_type: null, children: [] };
            dirMap.set(currentPath, dirNode);
            currentNode.children.push(dirNode);
          }
          currentNode = dirMap.get(currentPath)!;
        }
      }
    }
    const sortNode = (node: FileTreeNode) => {
      node.children.sort((a, b) => {
        if (a.type !== b.type) return a.type === 'directory' ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
      node.children.forEach(sortNode);
    };
    sortNode(root);
    return root;
  }, [skillFiles]);

  const toggleFileFolder = useCallback((path: string) => {
    setExpandedFileFolders(prev => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path); else next.add(path);
      return next;
    });
  }, []);

  const renderFileTree = useCallback((node: FileTreeNode, depth: number): JSX.Element | null => {
    if (node.type === 'file') {
      const previewable = isFilePreviewable(node);
      const selected = filePreviewPath === node.path;
      return (
        <button key={node.path} type="button"
          onClick={() => { if (previewable && selectedSkill) fetchFilePreview(selectedSkill.name, node.path); }}
          className={`w-full min-h-9 flex items-center gap-2 rounded-lg px-2 text-left text-sm border ${selected ? 'bg-accent-subtle text-text border-[var(--color-border-accent)]' : previewable ? 'text-text-muted hover:bg-secondary/40 hover:text-text border-transparent' : 'text-text-muted/60 border-transparent cursor-not-allowed'}`}
          style={{ paddingLeft: `${depth * 14 + 8}px` }} title={node.name}>
          <span className="w-4 h-4" />
          <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3.75h7.5l4.5 4.5v12a1.5 1.5 0 01-1.5 1.5h-10.5a1.5 1.5 0 01-1.5-1.5v-15a1.5 1.5 0 011.5-1.5zM14.25 3.75v4.5h4.5" /></svg>
          <span className="flex-1 min-w-0 truncate">{node.name}</span>
          {!previewable ? <span className="text-[10px] px-1.5 py-0.5 rounded border border-border bg-secondary/40">{t('skills.detail.notPreviewable')}</span> : null}
        </button>
      );
    }
    const isExpanded = expandedFileFolders.has(node.path);
    const hasChildren = node.children.length > 0;
    return (
      <div key={node.path || 'root'}>
        {node.path !== '' ? (
          <button type="button" onClick={() => toggleFileFolder(node.path)}
            className="w-full min-h-9 flex items-center gap-2 rounded-lg px-2 text-left text-sm text-text-muted hover:bg-secondary/40 hover:text-text"
            style={{ paddingLeft: `${depth * 14 + 8}px` }} title={node.name}>
            <span className="w-4 h-4 flex items-center justify-center text-text-muted/80">
              {hasChildren ? <svg className={`w-3 h-3 ${isExpanded ? 'rotate-90' : ''}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M9 6l6 6-6 6" /></svg> : null}
            </span>
            <svg className="w-4 h-4 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h4.5l1.5 2.25h10.5v8.25A2.25 2.25 0 0118 19.5H6A2.25 2.25 0 013.75 17.25V6.75z" /></svg>
            <span className="flex-1 min-w-0 truncate">{node.name}</span>
          </button>
        ) : null}
        {isExpanded || node.path === '' ? (
          <div>{node.children.map(child => renderFileTree(child, node.path === '' ? 0 : depth + 1))}</div>
        ) : null}
      </div>
    );
  }, [expandedFileFolders, filePreviewPath, selectedSkill, toggleFileFolder, fetchFilePreview, t]);

  const handleRebuild = useCallback(
    async (skillName: string, version: string | null) => {
      setRebuildLoading(true);
      showMessage("loading", t('skills.messages.rebuilding'));
      try {
        const data = await webRequest<SkillRebuildResponse>(
          "skills.rebuild",
          withSession({ name: skillName, version }),
          { timeoutMs: 5 * 60_000 }
        );
        if (data.success) {
          showMessage("success", t('skills.messages.rebuildCompleted'));
          fetchSkillDetail(skillName);
        }
      } catch (error) {
        console.error(error);
        const detail = error instanceof Error ? error.message : String(error);
        showMessage("error", t('skills.messages.rebuildFailed', { detail }));
      } finally {
        setRebuildLoading(false);
      }
    },
    [withSession, fetchSkillDetail, showMessage, t]
  );

  const startRetrievalIndexBuild = useCallback(async (force: boolean, useDefaultProfile = false) => {
    if (!isConnected) return false;
    try {
      const statusPayload = await webRequest<unknown>(
        "skills.retrieval.status",
        useDefaultProfile ? {} : withSession()
      );
      const status = parseSkillRetrievalStatus(statusPayload);
      if (status.build_status === "running") return true;
      if (!canBuildSkillRetrievalIndex(status)) return false;
      const buildParams = {
        force: force || status.index_exists,
        source: "web",
      };
      const payload = await webRequest<Record<string, unknown>>(
        "skills.retrieval.index_build",
        useDefaultProfile ? buildParams : withSession(buildParams),
        { timeoutMs: 30_000 }
      );
      if (payload.success !== true) {
        throw new Error(String(payload.detail || t('skills.retrieval.buildFailed')));
      }
      if (typeof payload.build_id !== "string" || !payload.build_id) {
        throw new Error(t('skills.retrieval.statusIncompatible'));
      }
      showMessage('success', t('skills.retrieval.buildStarted'));
      return true;
    } catch (error) {
      console.error('Failed to start Skill taxonomy build:', error);
      showMessage('error', error instanceof Error ? error.message : t('skills.retrieval.buildFailed'));
      return false;
    }
  }, [isConnected, showMessage, t, withSession]);

  useEffect(() => {
    const requestRevision = ++indexRecommendationRequestRef.current;
    if (!isActive || activeTab !== "graph" || !isConnected) {
      setIndexRecommendationVisible(false);
      return;
    }

    setIndexRecommendationVisible(false);
    void (async () => {
      try {
        const config = await webRequest<Record<string, unknown>>("config.get");
        if (requestRevision !== indexRecommendationRequestRef.current) return;
        if (
          !parseConfigBoolean(config.skill_retrieval_enabled)
          || parseConfigBoolean(config.skill_retrieval_index_enabled)
          || parseConfigBoolean(config.skill_retrieval_index_recommendation_shown)
        ) {
          return;
        }

        const status = parseSkillRetrievalStatus(
          await webRequest<unknown>("skills.retrieval.status")
        );
        if (requestRevision !== indexRecommendationRequestRef.current || !status.index_recommended) return;

        await webRequest("config.set", {
          skill_retrieval_index_recommendation_shown: "true",
        });
        if (requestRevision === indexRecommendationRequestRef.current) {
          setIndexRecommendationVisible(true);
        }
      } catch {
        // The recommendation is advisory and must not affect the Skill graph.
      }
    })();
  }, [activeTab, isActive, isConnected]);

  const buildRecommendedIndex = useCallback(async () => {
    if (indexRecommendationBuilding) return;
    setIndexRecommendationBuilding(true);
    try {
      await webRequest("config.set", {
        skill_retrieval_index_enabled: "true",
      });
      const started = await startRetrievalIndexBuild(false, true);
      if (started) {
        setIndexRecommendationVisible(false);
      } else {
        await webRequest("config.set", {
          skill_retrieval_index_enabled: "false",
        });
      }
    } catch (error) {
      console.error('Failed to enable Skill taxonomy index:', error);
      showMessage('error', t('skills.retrieval.buildFailed'));
    } finally {
      setIndexRecommendationBuilding(false);
    }
  }, [indexRecommendationBuilding, showMessage, startRetrievalIndexBuild, t]);

  // 当左边栏切换到技能页面时，或切换到"我的技能"页签时，调用 list 接口
  useEffect(() => {
    const prevIsActive = prevIsActiveRef.current;
    const isInitialMount = !mountedRef.current;
    mountedRef.current = true;

    // 场景1：从其他页面切换到技能页面（isActive 变为 true），或首次挂载且已激活
    if (isActive && (!prevIsActive || isInitialMount)) {
      fetchSkills();
    }

    // 场景2：在技能页面内切换到"我的技能"页签（isActive 保持 true，activeTab 变化）
    if (isActive && prevIsActive && activeTab === "my") {
      fetchSkills();
    }

    // 更新 ref
    prevIsActiveRef.current = isActive;
  }, [isActive, activeTab, fetchSkills, mountedRef]);

  // OAuth 回调后恢复技能详情页 + 打开发布抽屉
  // 在 isActive 变为 true 后执行（确保 WebSocket 已连接、SkillPanel 已挂载）
  useEffect(() => {
    if (!isActive) return;
    const oauthRedirect = sessionStorage.getItem('oauth_redirect');
    const skillName = sessionStorage.getItem('oauth_redirect_skill');
    if (oauthRedirect === 'publish' && skillName) {
      sessionStorage.removeItem('oauth_redirect');
      sessionStorage.removeItem('oauth_redirect_skill');
      const oauthError = sessionStorage.getItem('oauth_error');
      if (oauthError) {
        sessionStorage.removeItem('oauth_error');
        sessionStorage.removeItem('oauth_redirect_nav');
        setOauthError(oauthError);
        setOauthLoginOpen(true);
        return;
      }
      setActiveTab('my');
      fetchSkillDetail(skillName).then(() => {
        setPublishDrawerOpen(true);
      });
    }
  }, [isActive, fetchSkillDetail]);

  const handleOpenSkill = useCallback(
    (skillName: string) => {
      fetchSkillDetail(skillName);
    },
    [fetchSkillDetail]
  );

  const handleBackToList = useCallback(() => {
    setSelectedSkill(null);
    setDetailState("idle");
  }, []);

  // 新建会话并将技能选中到输入框
  const handleGoToChat = useCallback((skillName: string, skillType?: string) => {
    window.dispatchEvent(new CustomEvent('jiuwen:new-conversation', {
      detail: { skillName, ...(skillType === 'swarm_skill' ? { mode: 'team' as const } : {}) }
    }));
  }, []);

  const renderHubSkillActionButton = useCallback((skill: MarketplacePluginItem) => {
    if (installedSkillMap.has(skill.name)) {
      return (
        <button
          onClick={(e) => { e.stopPropagation(); handleGoToChat(skill.name, skill.plugin_type === 'swarmskill' ? 'swarm_skill' : undefined); }}
          onMouseEnter={(e) => {
            const rect = e.currentTarget.getBoundingClientRect();
            setGoTryTooltip({ left: rect.left + rect.width / 2, top: rect.top });
          }}
          onMouseLeave={() => setGoTryTooltip(null)}
          className="w-8 h-8 flex items-center justify-center rounded-[8px] bg-[var(--color-skill-card-action-surface)] hover:bg-[var(--color-skill-card-action-hover-surface)] text-text-muted hover:text-chat-accent transition-colors"
        >
          <NewConversationIcon aria-hidden width="20" height="20" />
        </button>
      );
    }
    return (
      <button
        onClick={(e) => { e.stopPropagation(); handleInstallHubSkill(skill); }}
        className="w-8 h-8 flex items-center justify-center rounded-[8px] bg-[var(--color-skill-card-action-surface)] hover:bg-[var(--color-skill-card-action-hover-surface)] text-text-muted hover:text-chat-accent transition-colors"
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14M5 12h14" />
        </svg>
      </button>
    );
  }, [installedSkillMap, handleGoToChat, handleInstallHubSkill]);

  // 新建会话：skill-creator（所有 Skill Creator 统一入口）chip + "帮我修改这个技能" + 该技能 chip
  const handleEditSkill = useCallback((skillName: string, skillType?: string) => {
    window.dispatchEvent(new CustomEvent('jiuwen:new-conversation', {
      detail: {
        skillName: 'skill-creator',
        suffixText: t('skills.chatPrompts.editSkill'),
        secondSkillName: skillName,
        ...(skillType === 'swarm_skill' ? { mode: 'team' as const } : {}),
        metadata: {
          scene: 'edit_skill',
          target_skill: skillName,
          ...(skillType ? { target_skill_type: skillType } : {}),
        }
      }
    }));
  }, [t]);

  // 通过聊天创建：新建会话，选中 skill-creator（统一入口）并在 chip 后追加创建提示文字
  const handleCreateViaChat = useCallback(() => {
    window.dispatchEvent(new CustomEvent('jiuwen:new-conversation', {
      detail: {
        skillName: 'skill-creator',
        suffixText: t('skills.chatPrompts.createSkill'),
        metadata: { scene: 'create_skill' }
      }
    }));
  }, [t]);

  // ---- 技能经验（内联展示） ----
  const sortedEvolutionEntries = useMemo(
    () =>
      [...evolutionEntries].sort((a, b) => {
        const ta = a.timestamp || "";
        const tb = b.timestamp || "";
        return tb.localeCompare(ta);
      }),
    [evolutionEntries]
  );

  const fetchEvolutionEntries = useCallback(async () => {
    if (!selectedSkill) return;
    setEvolutionListState("loading");
    setEvolutionMessage(null);
    setEvolutionMessageType(null);
    setEvolutionFormatError(null);
    try {
      const data = await webRequest<EvolutionGetResponse>(
        "skills.evolution.get",
        withSession({ name: selectedSkill.name })
      );
      if (!data.exists) {
        setEvolutionEntries([]);
        setEvolutionListState("success");
        return;
      }
      if (data.valid === false) {
        setEvolutionEntries([]);
        setEvolutionFormatError(data.detail || t("skills.evolution.errors.invalidFile"));
        setEvolutionListState("success");
        return;
      }
      setEvolutionEntries(data.entries || []);
      setEvolutionListState("success");
    } catch (error) {
      console.error(error);
      setEvolutionListState("error");
    }
  }, [selectedSkill, t, withSession]);

  useEffect(() => {
    if (detailTab === "experience" && selectedSkill?.has_evolutions) {
      void fetchEvolutionEntries();
    }
  }, [detailTab, selectedSkill, fetchEvolutionEntries]);

  const handleEvolutionContentChange = useCallback((entryId: string, value: string) => {
    setEvolutionEntries((prev) =>
      prev.map((entry) =>
        entry.id === entryId
          ? { ...entry, change: { ...entry.change, content: value } }
          : entry
      )
    );
  }, []);

  const handleEvolutionDeleteEntry = useCallback(
    (entryId: string) => {
      const confirmed = window.confirm(t("skills.evolution.deleteConfirm"));
      if (!confirmed) return;
      setEvolutionEntries((prev) => prev.filter((entry) => entry.id !== entryId));
    },
    [t]
  );

  // 自动保存（带防抖）
  const saveEvolutionEntries = useCallback(async (entries: EvolutionEntry[]) => {
    if (!selectedSkill) return;
    try {
      const data = await webRequest<{
        success: boolean;
        detail?: string;
        message?: string;
      }>("skills.evolution.save", withSession({ name: selectedSkill.name, entries }));
      if (!data.success) {
        throw new Error(data.detail || data.message || t("skills.evolution.errors.saveFailed"));
      }
      await fetchSkills();
    } catch (error) {
      console.error(error);
      setEvolutionMessage(t("skills.evolution.errors.saveFailed"));
      setEvolutionMessageType("error");
    }
  }, [selectedSkill, t, withSession, fetchSkills]);

  // 防抖保存：监听 evolutionEntries 变化
  useEffect(() => {
    // 只在技能经验页签且有数据时触发
    if (detailTab !== "experience" || !selectedSkill?.has_evolutions || evolutionEntries.length === 0) {
      return;
    }
    // 清除之前的计时器
    if (evolutionSaveTimerRef.current) {
      clearTimeout(evolutionSaveTimerRef.current);
    }
    // 设置新的防抖计时器
    evolutionSaveTimerRef.current = window.setTimeout(() => {
      saveEvolutionEntries(evolutionEntries);
    }, 500);
    // 清理函数
    return () => {
      if (evolutionSaveTimerRef.current) {
        clearTimeout(evolutionSaveTimerRef.current);
      }
    };
  }, [evolutionEntries, detailTab, selectedSkill, saveEvolutionEntries]);

  /** 上传技能 .zip 包：先上传到临时目录，再通过 WebSocket 调用 skills.import_upload */
  const handleSkillUpload = useCallback(async (file: File) => {
    setActionTarget("import_local");
    setMessage(null);
    setMessageType(null);
    try {
      // Step 1: 上传文件到 Vite dev server 临时目录
      const form = new FormData();
      form.append("file", file);
      const uploadResp = await fetch("/file-api/skills/upload-temp", { method: "POST", body: form });
      const uploadData = await uploadResp.json();
      if (!uploadResp.ok || !uploadData.path) {
        throw new Error(uploadData.error || t('skills.errors.importFailed'));
      }
      const tempPath = uploadData.path;

      // Step 2: 通过 WebSocket 调用 skills.import_upload
      const doImport = async (overwrite: boolean) => {
        const data = await webRequest<{
          success: boolean;
          detail?: string;
          message?: string;
          skill?: { name?: string };
          code?: string;
        }>("skills.import_upload", withSession({
          path: tempPath,
          overwrite,
        }));
        if (!data.success) {
          const err = new Error(data.detail || data.message || t('skills.errors.importFailed')) as Error & { code?: string };
          err.code = data.code;
          throw err;
        }
        return data;
      };

      let data;
      try {
        data = await doImport(false);
      } catch (error) {
        const code = (error as Error & { code?: string }).code;
        if (code === "SKILL_ALREADY_EXISTS" || code === "SKILL_IMPORT_OVERWRITE_REQUIRED") {
          const msg = error instanceof Error ? error.message : String(error);
          const overwrite = window.confirm(`${msg}\n${t('skills.overwriteConfirm')}`);
          if (!overwrite) return;
          data = await doImport(true);
        } else {
          throw error;
        }
      }

      showMessage("success", t('skills.messages.imported', { name: data.skill?.name || file.name }));
      await fetchSkills();
      if (data.skill?.name) {
        await fetchSkillDetail(data.skill.name);
      }
    } catch (error) {
      console.error(error);
      const errorMessage = error instanceof Error ? error.message : String(error);
      showMessage("error", errorMessage || t('skills.errors.importFailedHint'));
    } finally {
      setActionTarget(null);
    }
  }, [fetchSkills, fetchSkillDetail, t, withSession]);

  /** 知识转技能：先上传文件到临时目录（如有），再通过 WebSocket 调用 skills.create_from_knowledge */
  const handleCreateFromKnowledge = useCallback(async (params: {
    file?: File | null;
    link?: string;
    skillDescription?: string;
  }) => {
    setActionTarget("import_local");
    const newCount = knowledgeTaskCount + 1;
    setKnowledgeTaskCount(newCount);
    setMessage(null);
    setMessageType(null);
    showMessage("loading", newCount > 1
      ? t('skills.messages.knowledgeSkillCreatingCount', { count: newCount })
      : t('skills.messages.knowledgeSkillCreating'));
    try {
      let filePath = "";
      let link = "";

      if (params.file) {
        const form = new FormData();
        form.append("file", params.file);
        const uploadResp = await fetch("/file-api/skills/upload-temp", { method: "POST", body: form });
        const uploadData = await uploadResp.json();
        if (!uploadResp.ok || !uploadData.path) {
          throw new Error(uploadData.error || t('skills.errors.importFailed'));
        }
        filePath = uploadData.path;
      } else if (params.link) {
        link = params.link;
      } else {
        showMessage("error", t('skills.errors.importFailed'));
        return;
      }

      const data = await webRequest<{
        success: boolean;
        detail?: string;
        message?: string;
      }>("skills.create_from_knowledge", withSession({
        ...(filePath ? { file_path: filePath } : { link }),
        skill_description: params.skillDescription || "",
      }), { timeoutMs: 600000 });

      if (!data.success) {
        throw new Error(data.detail || data.message || t('skills.errors.importFailed'));
      }

      showMessage("success", t('skills.messages.knowledgeSkillCreated'));
      await fetchSkills();
    } catch (error) {
      console.error(error);
      const errorMessage = error instanceof Error ? error.message : String(error);
      showMessage("error", errorMessage || t('skills.errors.importFailedHint'));
    } finally {
      const remaining = knowledgeTaskCount - 1;
      setKnowledgeTaskCount(remaining);
      if (remaining <= 0) {
        setActionTarget(null);
      }
    }
  }, [fetchSkills, t, withSession, knowledgeTaskCount]);

  // ── 发布表单校验（与 skillhub 对齐） ──
  const validatePublishSkillName = useCallback((name: string): string | null => {
    const trimmed = name.trim();
    if (!trimmed) return null; // 空值由必填校验兜底
    if (trimmed.length > SKILL_NAME_MAX_LEN) {
      return t('skills.publishForm.errorNameTooLong', { max: SKILL_NAME_MAX_LEN });
    }
    if (!SKILL_NAME_PATTERN.test(trimmed)) {
      return t('skills.publishForm.errorInvalidName');
    }
    return null;
  }, [t]);

  const validatePublishVersion = useCallback((version: string): string | null => {
    const trimmed = version.trim();
    if (!trimmed) return null;
    if (!VERSION_PATTERN.test(trimmed)) {
      return t('skills.publishForm.errorInvalidVersion');
    }
    return null;
  }, [t]);

  const validatePublishDisplayName = useCallback((displayName: string): string | null => {
    const trimmed = displayName.trim();
    if (!trimmed) return null;
    if (trimmed.length > DISPLAY_NAME_MAX_LEN) {
      return t('skills.publishForm.errorDisplayNameTooLong', { max: DISPLAY_NAME_MAX_LEN });
    }
    return null;
  }, [t]);

  const handlePublish = useCallback(async () => {
    if (!selectedSkill) return;
    const token = getStoredOAuthToken();
    if (!token) {
      setOauthLoginOpen(true);
      return;
    }
    // 提交前校验（与 skillhub 对齐）
    const errors: Record<string, string> = {};
    const nameErr = validatePublishSkillName(publishSkillName);
    if (nameErr) errors.skillName = nameErr;
    const verErr = validatePublishVersion(publishVersion);
    if (verErr) errors.version = verErr;
    const dnErr = validatePublishDisplayName(publishDisplayName);
    if (dnErr) errors.displayName = dnErr;
    if (Object.keys(errors).length > 0) {
      setPublishFieldErrors(errors);
      return;
    }
    setPublishFieldErrors({});
    setPublishError(null);
    setActionTarget("publish");
    try {
      // 1. 从 file_path 推导技能目录
      const filePath = (selectedSkill as SkillDetail).file_path || '';
      const skillDir = filePath ? filePath.replace(/[\\/][^\\/]+$/, '') : '';
      if (!skillDir) {
        setPublishError(t('skills.publishForm.pathNotFound'));
        return;
      }

      // 2. WS 打包 zip（后端打包，前端下载）
      const packResult = await webRequest<{ success: boolean; path?: string; checksum_sha256?: string; detail?: string }>(
        "skills.teamskillshub.pack",
        withSession({
          path: skillDir,
          output: "out",
          version: publishVersion,
          skill_name: publishSkillName,
          display_name: publishDisplayName,
        }),
        { timeoutMs: 60000 }
      );
      if (!packResult.success || !packResult.path) {
        setPublishError(packResult.detail || t('skills.messages.packFailed'));
        return;
      }

      // 3. 下载 zip 为 Blob
      const zipResp = await fetch(`/file-api/raw-file?path=${encodeURIComponent(packResult.path)}`);
      if (!zipResp.ok) {
        setPublishError(t('skills.messages.downloadFailed'));
        return;
      }
      const zipBlob = await zipResp.blob();

      // 4. 使用后端返回的 SHA256（避免 crypto.subtle 在非安全上下文不可用）
      const checksum = packResult.checksum_sha256 || '';

      // 5. 组装 FormData（前端组装，与 skillhub 对齐）
      const formData = new FormData();
      formData.append('file', zipBlob, `${selectedSkill.name}.zip`);
      formData.append('plugin_version', publishVersion);
      if (publishVersionDesc) formData.append('version_desc', publishVersionDesc);
      if (publishForce) formData.append('force', 'true');

      // 6. POST 到 Hub（通过 Vite /hub-api/ 代理）
      const provider = getStoredOAuthProvider();
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 120000);
      const resp = await fetch('/hub-api/api/v1/plugins', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'X-OAuth-Provider': provider,
          'X-Checksum-SHA256': checksum,
        },
        body: formData,
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      const respData = await resp.json();

      if (respData.code === 200 && respData.data?.plugin_id) {
        showMessage("success", t('skills.messages.published', {
          name: publishDisplayName || respData.data?.display_name || respData.data?.name || selectedSkill.name,
        }));
        setPublishDrawerOpen(false);
        setPublishVersion("");
        setPublishVersionDesc("");
        setPublishFieldErrors({});
        setPublishError(null);
        await fetchSkills();
      } else {
        // 检查版本冲突
        const detail = respData.detail || {};
        const errorMsg = detail.message || detail.error || respData.message || '';
        setPublishError(errorMsg || t('skills.messages.publishFailed'));
      }
    } catch (error) {
      const errMsg = error instanceof Error ? error.message : '';
      setPublishError(errMsg || t('skills.messages.publishFailed'));
    } finally {
      setActionTarget(null);
    }
  }, [selectedSkill, publishVersion, publishVersionDesc, publishSkillName, publishDisplayName, publishForce, withSession, fetchSkills, t, validatePublishSkillName, validatePublishVersion, validatePublishDisplayName]);

  const handleUninstall = useCallback(
    async (pluginName: string) => {
      if (!pluginName) return;
      const confirmed = window.confirm(t('skills.uninstallConfirm', { pluginName }));
      if (!confirmed) return;

      setActionTarget(pluginName);
      setMessage(null);
      setMessageType(null);
      try {
        const data = await webRequest<{
          success: boolean;
          detail?: string;
          message?: string;
        }>("skills.uninstall", withSession({
          name: pluginName,
        }));
        if (!data.success) {
          throw new Error(data.detail || data.message || t('skills.errors.uninstallFailed'));
        }
        showMessage("success", t('skills.messages.uninstalled', { pluginName }));
        await fetchSkills();
        handleBackToList();
      } catch (error) {
        console.error(error);
        const errorMessage = error instanceof Error ? error.message : String(error);
        showMessage("error", errorMessage || t('skills.errors.uninstallFailedHint'));
      } finally {
        setActionTarget(null);
      }
    },
    [fetchSkills, handleBackToList, t, withSession]
  );

  // 2026-08-25：改用 utils/mySkills.ts 的共享 isSkillInstalled/filterEnabledMySkills，跟"手动创建
  // 插件"的"添加技能"弹窗（CreatePluginPage.tsx）共用同一份"已启用"判定规则，见该文件头注释。
  const mySkillsFiltered = useMemo(() => {
    let filtered = visibleSkills;
    switch (mySkillsSubTab) {
      case "enabled":
        filtered = filterEnabledMySkills(visibleSkills, installedSkillNames);
        break;
      case "disabled":
        filtered = filtered.filter(s => s.enabled === false);
        break;
      default:
        break;
    }
    // 发布状态筛选
    if (mySkillsPublishFilter === "published") {
      filtered = filtered.filter(s => s.published === true);
    } else if (mySkillsPublishFilter === "unpublished") {
      filtered = filtered.filter(s => s.published !== true);
    }
    return filtered;
  }, [visibleSkills, mySkillsSubTab, mySkillsPublishFilter, installedSkillNames]);

  const toggleSkillDisabled = async (skillName: string) => {
    const skill = skills.find(s => s.name === skillName);
    const newEnabled = skill?.enabled === false ? true : false;
    
    const toggleKey = `toggle:${skillName}`;
    setActionTarget(toggleKey);
    
    try {
      const result = await webRequest<{
        success: boolean;
        name: string;
        enabled: boolean;
        detail?: string;
      }>(
        "skills.toggle",
        withSession({ name: skillName, enabled: newEnabled })
      );
      
      if (!result.success) {
        throw new Error(result.detail || 'Failed to toggle skill');
      }
      
      setSkills((prev) => 
        prev.map(s => 
          s.name === skillName ? { ...s, enabled: newEnabled } : s
        )
      );
      
      if (selectedSkill && selectedSkill.name === skillName) {
        setSelectedSkill({ ...selectedSkill, enabled: newEnabled });
      }
    } catch (error) {
      console.error('Failed to toggle skill enabled:', error);
      showMessage('error', t('skills.setEnabledError'));
    } finally {
      setActionTarget(null);
    }
  };

  /** 判断技能是否为技能包（技能名与所属插件名一致） */
  const isSkillPackage = useCallback((skill: SkillItem): boolean => {
    const plugin = installedSkillMap.get(skill.name);
    return Boolean(plugin && plugin.plugin_name === skill.name && plugin.skills.length > 1);
  }, [installedSkillMap]);

  const cleanMessage = message?.replace("√", "") || "";

  const detailContent = useMemo(
    () =>
      selectedSkill?.content
        ? transformSkillContentImages(selectedSkill.content, selectedSkill.file_path)
        : '',
    [selectedSkill]
  );
  // ── OAuth 登录逻辑（当前页跳转） ──

  // 跳转到 OAuth 授权页（当前页跳转，登录后回调 /oauth/callback?code=xxx）
  // provider: 'gitcode' | 'github'
  const handleOAuthLogin = useCallback((provider: OAuthProvider = 'gitcode') => {
    setOauthError(null);
    setOauthLoadingProvider(provider);
    sessionStorage.setItem('oauth_redirect', 'publish');
    sessionStorage.setItem('oauth_redirect_nav', 'skills');
    // 保存当前技能名，OAuth 回调后恢复详情页
    if (selectedSkill?.name) {
      sessionStorage.setItem('oauth_redirect_skill', selectedSkill.name);
    }
    window.location.href = buildOAuthUrl(provider);
  }, [t, selectedSkill]);

  // 监听 OAuth 回调完成事件（App.tsx 处理完 code → token 后触发）
  // 如果是从发布弹窗触发的登录，恢复技能详情页并打开发布抽屉
  useEffect(() => {
    const handleOAuthComplete = () => {
      // 检查是否有 OAuth 错误（Client ID/Secret 不正确等）
      const oauthError = sessionStorage.getItem('oauth_error');
      if (oauthError) {
        sessionStorage.removeItem('oauth_error');
        sessionStorage.removeItem('oauth_redirect');
        sessionStorage.removeItem('oauth_redirect_nav');
        sessionStorage.removeItem('oauth_redirect_skill');
        setOauthError(oauthError);
        setOauthLoginOpen(true); // 重新打开弹窗显示错误
        return;
      }
    };
    window.addEventListener('oauth-callback-complete', handleOAuthComplete);
    return () => window.removeEventListener('oauth-callback-complete', handleOAuthComplete);
  }, []);

  return (
    <>
      {message && messageType === "success" && (
        <div className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }} data-testid="skill-panel-toast" data-variant="success">
          <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
            <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
            </svg>
          </span>
          {cleanMessage}
          <button
            type="button"
            onClick={() => setMessage(null)}
            className="ml-auto w-5 h-5 flex items-center justify-center hover:bg-card/30 rounded-full "
            data-testid="skill-panel-toast-close-btn"
          >
            <svg className="w-4 h-4 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      )}
      {message && messageType === "loading" && (
        <div className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4 bg-card border border-border" style={{ width: "564px", height: "40px" }} data-testid="skill-panel-toast" data-variant="loading">
          <span className="w-4 h-4 border-2 border-accent border-t-transparent rounded-full animate-spin flex-shrink-0" />
          {cleanMessage}
        </div>
      )}
      <div className="app-page-body">
        <div
          className="page-content"
          data-testid="skill-content"
        >
          {!(activeTab === "my" && selectedSkill) && !(activeTab === "marketplace" && marketplaceSubView === 'detail') && (
          <>
          <PageHeader title={t('skills.title')} subtitle={t('skills.subtitle')}>
            <button
              onClick={() => setSourceModalOpen(true)}
              className="flex items-center gap-1.5 px-1 py-1.5 rounded-lg text-sm text-text-muted hover:text-text hover:bg-secondary/50 "
              data-testid="skill-panel-source-manager-btn"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
              </svg>
              {t('skills.actions.sourceManager')}
            </button>
            <button
              onClick={() => {
                if (activeTab === "graph") {
                  const started = skillGraphPanelRef.current?.refresh() ?? false;
                  if (started) {
                    updateGraphReading(true);
                  }
                } else if (activeTab === "my" || activeTab === "marketplace") {
                  setSearch("");
                  fetchSkills(true);
                }
              }}
              className={`flex items-center gap-1.5 pl-[18px] pr-[24px] py-1.5 rounded-lg text-sm text-text-muted  ${
                activeTab === "graph" && graphReading
                  ? "cursor-not-allowed opacity-70"
                  : "hover:text-text hover:bg-secondary/50"
              }`}
              disabled={activeTab === "graph" && graphReading}
              data-testid="skill-panel-refresh-btn"
            >
              <svg className={`w-4 h-4 ${activeTab === "graph" && graphReading ? "animate-spin" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8" />
                <path d="M21 3v5h-5" />
              </svg>
              {activeTab === "graph" && graphReading ? t('skills.graph.status.reading') : t('common.refresh')}
            </button>
          </PageHeader>

        <div className="page-toolbar" data-testid="page-toolbar">
          <div className="chat-picker-panel__tabs">
            <button
              type="button"
              onClick={() => setActiveTab("marketplace")}
              className={activeTab === "marketplace" ? "is-active" : ""}
              data-testid="skill-panel-tab"
              data-variant="marketplace"
            >
              {t('skills.tabs.marketplace')}
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("my")}
              className={activeTab === "my" ? "is-active" : ""}
              data-testid="skill-panel-tab"
              data-variant="my"
            >
              {t('skills.tabs.mySkills')}
            </button>
            <button
              type="button"
              onClick={() => setActiveTab("graph")}
              className={activeTab === "graph" ? "is-active" : ""}
              data-testid="skill-panel-tab"
              data-variant="graph"
            >
              {t('skills.tabs.skillGraph')}
            </button>
          </div>
          <div className="flex items-center gap-3 " >
            {activeTab === "my" && (
              <>
                {/* 已发布/未发布筛选 */}
                <FilterDropdown
                  open={publishFilterOpen}
                  onToggle={(v) => { setPublishFilterOpen(v); setEnableFilterOpen(false); }}
                  onClose={() => setPublishFilterOpen(false)}
                  value={mySkillsPublishFilter}
                  onChange={(v) => { setMySkillsPublishFilter(v); setPublishFilterOpen(false); }}
                  options={[
                    { value: 'all', label: t('skills.publishFilter.all') },
                    { value: 'published', label: t('skills.publishFilter.published') },
                    { value: 'unpublished', label: t('skills.publishFilter.unpublished') },
                  ]}
                  style={{ width: mySkillsPublishFilter === "all" ? '40px' : '55px' }}
                />
                {/* 启用/禁用筛选 */}
                <FilterDropdown
                  open={enableFilterOpen}
                  onToggle={(v) => { setEnableFilterOpen(v); setPublishFilterOpen(false); }}
                  onClose={() => setEnableFilterOpen(false)}
                  value={mySkillsSubTab}
                  onChange={(v) => { setMySkillsSubTab(v); setEnableFilterOpen(false); }}
                  options={[
                    { value: 'all', label: t('skills.mySkillsTabs.all') },
                    { value: 'enabled', label: t('skills.mySkillsTabs.enabled') },
                    { value: 'disabled', label: t('skills.mySkillsTabs.disabled') },
                  ]}
                  style={{ width: '40px' }}
                />
              </>
            )}
            {(activeTab === "my" || activeTab === "marketplace") && (
              <PageToolbarSearch
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder={t('skills.searchPlaceholder')}
                inputTestId="skill-panel-search-input"
              />
            )}
            {activeTab === "my" && (
              <div className="relative">
                <button
                  onClick={() => setCreateMenuOpen((v) => !v)}
                  className="flex items-center justify-center gap-1 h-8 w-[96px] rounded-[16px] text-sm text-text-inverse bg-control-emphasis hover:opacity-80"
                  data-testid="skill-panel-create-btn"
                >
                  {t('skills.actions.create')}
                  <svg className={`w-3.5 h-3.5 transition-transform ${createMenuOpen ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                  </svg>
                </button>
                {createMenuOpen && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={() => setCreateMenuOpen(false)} />
                    <div className="absolute right-0 top-full mt-1 z-50 min-w-[160px] rounded-lg border border-border bg-panel shadow-lg py-1">
                      <button
                        onClick={() => {
                          setCreateMenuOpen(false);
                          setUploadSkillModalOpen(true);
                        }}
                        disabled={actionTarget === "import_local"}
                        className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary disabled:opacity-60 disabled:cursor-not-allowed"
                        data-testid="skill-panel-create-menu-item"
                        data-variant="upload-local"
                      >
                        {t('skills.actions.uploadLocalSkill')}
                      </button>
                      <button
                        onClick={() => {
                          setCreateMenuOpen(false);
                          setDocToSkillModalOpen(true);
                        }}
                        className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary"
                        data-testid="skill-panel-create-menu-item"
                        data-variant="doc-to-skill"
                      >
                        {t('skills.actions.documentToSkill')}
                      </button>
                      <button
                        onClick={() => {
                          setCreateMenuOpen(false);
                          handleCreateViaChat();
                        }}
                        className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary"
                        data-testid="skill-panel-create-menu-item"
                        data-variant="via-chat"
                      >
                        {t('skills.actions.createViaChat')}
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
          </>
          )}

        {activeTab === "graph" ? (
          <div data-testid="skill-panel-graph-view" className="mt-4 flex flex-1 min-h-0 flex-col gap-3">
            {indexRecommendationVisible ? (
              <div
                className="flex flex-none flex-col gap-3 rounded-lg border border-warn bg-warn-subtle px-4 py-3"
                data-testid="skill-index-recommendation"
              >
                <p className="whitespace-pre-line text-sm leading-6 text-text">
                  {t('skills.retrieval.indexRecommended')}
                </p>
                <div className="flex justify-end gap-2">
                  <button
                    type="button"
                    className="rounded-lg border border-border bg-panel px-4 py-2 text-sm text-text hover:bg-secondary/50 disabled:cursor-not-allowed disabled:opacity-60"
                    disabled={indexRecommendationBuilding}
                    onClick={() => setIndexRecommendationVisible(false)}
                  >
                    {t('skills.retrieval.recommendationDismiss')}
                  </button>
                  <button
                    type="button"
                    className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
                    disabled={indexRecommendationBuilding}
                    onClick={() => void buildRecommendedIndex()}
                  >
                    {indexRecommendationBuilding ? <Loader2 size={15} className="animate-spin" /> : null}
                    {t('skills.retrieval.recommendationBuild')}
                  </button>
                </div>
              </div>
            ) : null}
            <div data-testid="skill-panel-graph-orchestration-card" className="flex flex-none flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-panel p-4">
              <div className="min-w-[240px] flex-1">
                <div className="flex items-start gap-2">
                  <Music2 size={28} className="mt-1 flex-shrink-0 text-accent" aria-hidden="true" />
                  <div className="min-w-0">
                    <p data-testid="skill-panel-graph-definition" className="text-xs leading-5 text-text-muted">
                      {t('skills.graph.orchestration.graphDefinition')}
                    </p>
                    <p data-testid="skill-panel-graph-orchestration-description" className="text-xs leading-5 text-text-muted">
                      {t('skills.graph.orchestration.description')}
                    </p>
                  </div>
                </div>
                {symphonySaveError ? (
                  <p className="mt-1 text-xs leading-5 text-danger" role="alert">
                    {symphonySaveError}
                  </p>
                ) : null}
              </div>
              <div className="flex flex-shrink-0 items-center gap-2">
                {symphonySaving ? (
                  <>
                    <Loader2 size={16} className="animate-spin text-text-muted" aria-hidden="true" />
                    <span className="text-xs text-text-muted">
                      {t('skills.graph.orchestration.saving')}
                    </span>
                  </>
                ) : null}
                <Switch
                  checked={symphonyEnabledDraft}
                  onChange={(enabled) => void updateSymphonyEnabled(enabled)}
                  disabled={!isConnected || symphonySaving}
                  title={t(
                    isConnected
                      ? 'skills.graph.orchestration.toggleLabel'
                      : 'skills.graph.orchestration.connectionRequired',
                  )}
                />
              </div>
            </div>
            <div className="flex-1 min-h-0">
              <SkillGraphPanel
                ref={skillGraphPanelRef}
                onReadingChange={updateGraphReading}
                onBuildAccepted={(mode) => void startRetrievalIndexBuild(mode === 'full')}
                externalError={graphActionError}
                onExternalErrorClear={clearGraphActionError}
              />
            </div>
          </div>
        ) : null}

        {activeTab === "marketplace" ? (
          <>
          {marketplaceSubView === 'detail' && selectedHubSkill ? (
            /* 广场技能详情页 */
            <div className="mt-4 flex-1 flex flex-col overflow-y-auto" data-testid="skill-panel-hub-detail">
              {hubDetailState === "loading" && (
                <div className="text-sm text-text-muted mb-3" data-testid="skill-panel-hub-detail-state" data-variant="loading">{t('skills.detailLoading')}</div>
              )}
              {hubDetailState === "error" && (
                <div className="text-sm text-text-muted mb-3" data-testid="skill-panel-hub-detail-state" data-variant="error">{t('skills.detailError')}</div>
              )}

              {/* 面包屑 */}
              <div className="flex items-center gap-1.5 text-sm text-text-muted mb-4" data-testid="skill-panel-hub-detail-breadcrumb">
                <span>{t('skills.title')}</span>
                <span className="inline-flex items-center h-4 text-text-divider">/</span>
                <span>{t('skills.tabs.marketplace')}</span>
                <span className="inline-flex items-center h-4 text-text-divider">/</span>
                <span className="text-text truncate">{selectedHubSkill.display_name || selectedHubSkill.name}</span>
              </div>

              {/* 顶部：返回按钮 + 头像/名称 + 下载按钮 */}
              <div className="flex items-center justify-between gap-4 mb-6">
                <div className="flex items-center gap-3 min-w-0">
                  <button
                    onClick={() => { setMarketplaceSubView('list'); setSelectedHubSkill(null); setHubDetail(null); setHubDetailState('idle'); }}
                    className="flex-shrink-0 w-9 h-9 rounded-lg flex items-center justify-center text-text-muted hover:text-text hover:bg-secondary/50"
                    data-testid="skill-panel-hub-detail-back-btn"
                  >
                    <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                      <path d="m12 19-7-7 7-7" />
                      <path d="M19 12H5" />
                    </svg>
                  </button>
                  <div className={`w-12 h-12 rounded-lg ${getSkillAvatar(selectedHubSkill.name).color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
                    {getSkillAvatar(selectedHubSkill.name).firstChar}
                  </div>
                  <div className="min-w-0">
                    <span className="text-lg font-semibold text-text-strong truncate" data-testid="skill-panel-hub-detail-name">
                      {selectedHubSkill.display_name || selectedHubSkill.name}
                    </span>
                  </div>
                </div>

                {/* 下载/去试试按钮 */}
                <div className="flex items-center gap-2 flex-shrink-0">
                  {(() => {
                    const isInstalled = installedSkillMap.has(selectedHubSkill.name);
                    if (isInstalled) {
                      return (
                        <button
                          onClick={() => handleGoToChat(selectedHubSkill.name, selectedHubSkill.plugin_type === 'swarmskill' ? 'swarm_skill' : undefined)}
                          className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
                          style={{ height: '32px', padding: '0 24px' }}
                          data-testid="skill-panel-hub-detail-go-try-btn"
                        >
                          {t('skills.actions.goTry')}
                        </button>
                      );
                    }
                    return (
                      <button
                        onClick={() => handleInstallHubSkill(selectedHubSkill)}
                        disabled={actionTarget === `install:${selectedHubSkill.identifier || selectedHubSkill.asset_id}`}
                        className="flex items-center justify-center rounded-[16px] text-sm text-text-inverse bg-control-emphasis hover:bg-control-emphasis-hover-strong whitespace-nowrap disabled:opacity-50"
                        style={{ width: '96px', height: '32px' }}
                        data-testid="skill-panel-hub-detail-install-btn"
                      >
                        {actionTarget === `install:${selectedHubSkill.identifier || selectedHubSkill.asset_id}` ? t('common.processing') : t('skills.actions.install')}
                      </button>
                    );
                  })()}
                </div>
              </div>

              {/* 基本信息 */}
              <div className="mb-6" data-testid="skill-panel-hub-detail-basic-info">
                <div className="text-sm font-semibold text-text mb-2">
                  {t('skills.detail.basicInfo')}
                </div>
                <div className="text-sm text-text-muted">
                  {hubDetail?.data?.short_desc || selectedHubSkill.short_desc || t('skills.noDescription')}
                </div>
              </div>

              {/* 仅内容详情页签 */}
              <div className="flex-1 flex flex-col min-h-0">
                <div className="flex items-center mb-4 flex-shrink-0">
                  <div className="flex items-center gap-8 flex-1 border-b border-border">
                    <button
                      className="pb-2 text-sm text-text font-semibold border-b-2 border-text"
                      data-testid="skill-panel-hub-detail-tab"
                      data-variant="content"
                    >
                      {t('skills.detail.tabs.contentDetail')}
                    </button>
                  </div>
                </div>

                {/* 内容详情 */}
                <div className="flex-1 min-h-0 overflow-y-auto text-sm text-text bg-secondary border border-border rounded-md p-3" data-testid="skill-panel-hub-detail-content">
                  {hubDetail?.data?.detail_desc ? (
                    <MarkdownRenderer content={hubDetail.data.detail_desc} className="chat-text chat-markdown" />
                  ) : (
                    t('skills.noContent')
                  )}
                </div>
              </div>
            </div>
          ) : marketplaceSubView === 'team' ? (
            /* 精选团队技能专页 */
            <div className="mt-4 flex-1 flex flex-col overflow-y-auto" data-testid="skill-panel-team-skills-page">
              {/* 面包屑 + 返回 */}
              <div className="flex items-center gap-1.5 text-sm text-text-muted mb-4">
                <button
                  onClick={() => setMarketplaceSubView('list')}
                  className="flex-shrink-0 w-9 h-9 rounded-lg flex items-center justify-center text-text-muted hover:text-text hover:bg-secondary/50"
                  data-testid="skill-panel-team-skills-back-btn"
                >
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                    <path d="m12 19-7-7 7-7" />
                    <path d="M19 12H5" />
                  </svg>
                </button>
                <span>{t('nav.skills')}</span>
                <span className="inline-flex items-center h-4 text-text-divider">/</span>
                <span>{t('skills.tabs.marketplace')}</span>
                <span className="inline-flex items-center h-4 text-text-divider">/</span>
                <span className="text-text truncate">{t('skills.featuredTeamSkills')}</span>
              </div>

              <div className="flex items-center justify-between mb-3">
                <span className="font-bold text-text-strong" style={{ fontSize: '16px' }} data-testid="skill-panel-team-skills-title">{t('skills.featuredTeamSkills')}</span>
              </div>
              <div className="card-grid-auto">
                {teamSkills.map((skill) => (
                  <HubSkillCard
                    key={skill.asset_id}
                    skill={skill}
                    onSelect={() => { setSelectedHubSkill(skill); fetchHubSkillDetail(skill); }}
                    action={renderHubSkillActionButton(skill)}
                  />
))}
              </div>
            </div>
          ) : (
            /* 默认列表视图 */
            <>
              <CategoryTabs
                items={MARKETPLACE_CATEGORIES.map(cat => ({ value: cat, label: t(`skills.marketplaceCategories.${cat}`) }))}
                value={marketplaceCategory}
                onChange={setMarketplaceCategory}
              />

              {hubLoading ? (
                <div className="mt-4 text-sm text-text-muted" data-testid="skill-panel-hub-list-loading">{t('common.loading')}</div>
              ) : hubSkills.length === 0 ? (
                <div className="mt-4 text-sm text-text-muted" data-testid="skill-panel-hub-list-empty">{t('skills.noMatches')}</div>
              ) : search.trim() ? (
                /* 搜索结果：全部罗列 */
                <div className="card-grid-auto mt-4 flex-1 min-h-0 overflow-y-auto">
                  {hubSkills.map((skill) => (
                    <HubSkillCard
                      key={skill.asset_id}
                      skill={skill}
                      onSelect={() => { setSelectedHubSkill(skill); fetchHubSkillDetail(skill); }}
                      action={renderHubSkillActionButton(skill)}
                    />
))}
                </div>
              ) : (
                /* 无搜索词：按 plugin_type 分组展示 */
                <div className="mt-4 flex-1 min-h-0 overflow-y-auto">
                  {/* 精选团队技能（最多一行，右侧"更多"） */}
                  {teamSkills.length > 0 && (
                    <>
                      <div className="flex items-center justify-between mb-3">
                        <span className="font-bold text-text-strong" style={{ fontSize: '16px' }}>{t('skills.featuredTeamSkills')}</span>
                        {teamSkills.length > 3 && (
                          <button
                            onClick={() => setMarketplaceSubView('team')}
                            className="flex items-center gap-0.5 text-sm text-text-muted hover:text-text"
                            data-testid="skill-panel-team-skills-more-btn"
                          >
                            {t('nav.more')}
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                              <path d="m9 18 6-6-6-6" />
                            </svg>
                          </button>
                        )}
                      </div>
                      <div className="card-grid-auto mb-6">
                        {visibleTeamSkills.map((skill) => (
                          <HubSkillCard
                            key={skill.asset_id}
                            skill={skill}
                            onSelect={() => { setSelectedHubSkill(skill); fetchHubSkillDetail(skill); }}
                            action={renderHubSkillActionButton(skill)}
                          />
))}
                      </div>
                    </>
                  )}

                  {/* 精选技能（全部罗列） */}
                  {featuredSkills.length > 0 && (
                    <>
                      <div className="flex items-center justify-between mb-3">
                        <span className="font-bold text-text-strong" style={{ fontSize: '16px' }} data-testid="skill-panel-featured-skills-title">{t('skills.featuredSkills')}</span>
                      </div>
                 <div className="card-grid-auto">
                        {featuredSkills.map((skill) => (
                          <HubSkillCard
                            key={skill.asset_id}
                            skill={skill}
                            onSelect={() => { setSelectedHubSkill(skill); fetchHubSkillDetail(skill); }}
                            action={renderHubSkillActionButton(skill)}
                          />
))}
                      </div>
                    </>
                  )}
                </div>
              )}
            </>
          )}
          </>
        ) : null}

        {activeTab === "my" ? (
          <>
            {message && messageType === "error" && (
              <div className="mt-3 px-3 py-2 rounded-md bg-secondary text-sm text-danger" data-testid="skill-panel-my-error">
                {message}
              </div>
            )}
            {selectedSkill ? (
              <div className="mt-4 flex-1 flex flex-col overflow-y-auto" data-testid="skill-panel-my-detail">
                {/* 加载/错误状态 */}
                {detailState === "loading" && (
                  <div className="text-sm text-text-muted mb-3" data-testid="skill-panel-my-detail-state" data-variant="loading">{t('skills.detailLoading')}</div>
                )}
                {detailState === "error" && (
                  <div className="text-sm text-text-muted mb-3" data-testid="skill-panel-my-detail-state" data-variant="error">{t('skills.detailError')}</div>
                )}

                {/* 面包屑 */}
                <div className="flex items-center gap-1.5 text-sm text-text-muted mb-4" data-testid="skill-panel-my-detail-breadcrumb">
                  <span>{t('skills.title')}</span>
                  <span className="inline-flex items-center h-4 text-text-divider">/</span>
                  <span>{t('skills.tabs.mySkills')}</span>
                  <span className="inline-flex items-center h-4 text-text-divider">/</span>
                  <span className="text-text truncate">{selectedSkill.display_name || selectedSkill.name}</span>
                </div>

                {/* 顶部：返回按钮 + 头像/名称/演进icon + 来源tag + 操作按钮 */}
                <div className="flex items-center justify-between gap-4 mb-6">
                  <div className="flex items-center gap-3 min-w-0">
                    <button
                      onClick={handleBackToList}
                      className="flex-shrink-0 w-9 h-9 rounded-lg flex items-center justify-center text-text-muted hover:text-text hover:bg-secondary/50"
                      data-testid="skill-panel-my-detail-back-btn"
                    >
                      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
                        <path d="m12 19-7-7 7-7" />
                        <path d="M19 12H5" />
                      </svg>
                    </button>
                    <div className={`w-12 h-12 rounded-lg ${getSkillAvatar(selectedSkill.name).color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
                      {getSkillAvatar(selectedSkill.name).firstChar}
                    </div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-lg font-semibold text-text-strong truncate" data-testid="skill-panel-my-detail-name">
                          {selectedSkill.display_name || selectedSkill.name}
                        </span>
                        {selectedSkill.has_evolutions ? (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setDetailTab("experience");
                            }}
                            className="relative shrink-0 w-5 h-5 flex items-center justify-center text-text-muted hover:text-text"
                            title={t('skills.actions.viewEvolution')}
                            data-testid="skill-panel-my-detail-evolution-btn"
                          >
                            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M11.68 2.009A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673c-.824-.85-1.678-1.731-2.21-3.348"/><circle cx="18" cy="5" r="3"/></svg>
                          </button>
                        ) : null}
                      </div>
                      <div className="mt-1 flex items-center gap-1.5 flex-wrap">
                        <SkillTypeBadge skillType={selectedSkill.skill_type} />
                      </div>
                    </div>
                  </div>

                  {/* 右侧操作按钮 */}
                  <div className="flex items-center gap-6 flex-shrink-0">
                    {/* ... 菜单：编辑/卸载 */}
                    <div className="relative">
                      <button
                        type="button"
                        onClick={() => setDetailMenuOpen((v) => !v)}
                        className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text-muted hover:text-text"
                        data-testid="skill-panel-my-detail-menu"
                      >
                        <MoreIcon aria-hidden />
                      </button>
                      {detailMenuOpen ? (
                        <>
                          <div className="fixed inset-0 z-40" onClick={() => setDetailMenuOpen(false)} />
                          <div className="absolute right-0 top-full mt-1 z-50 min-w-[160px] rounded-lg border border-border bg-panel shadow-lg py-1">
                            <button
                              onClick={selectedSkill.enabled !== false ? () => {
                                setDetailMenuOpen(false);
                                handleEditSkill(selectedSkill.name, selectedSkill.skill_type);
                              } : undefined}
                              disabled={selectedSkill.enabled === false}
                              className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                              data-testid="skill-panel-my-detail-menu-edit"
                            >
                              {t('skills.actions.edit')}
                            </button>
                            <button
                              onClick={() => {
                                setDetailMenuOpen(false);
                                const plugin = installedSkillMap.get(selectedSkill.name);
                                handleUninstall(plugin?.plugin_name || selectedSkill.name);
                              }}
                              className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary"
                              data-testid="skill-panel-my-detail-menu-uninstall"
                            >
                              {t('skills.actions.uninstall')}
                            </button>
                          </div>
                        </>
                      ) : null}
                    </div>
                    {/* 启用开关 + 文字 */}
                    <div className="flex items-center gap-2">
                      <Switch
                        checked={selectedSkill.enabled !== false}
                        onChange={() => toggleSkillDisabled(selectedSkill.name)}
                        disabled={actionTarget === `toggle:${selectedSkill.name}`}
                      />
                      <span className="text-sm text-text-muted whitespace-nowrap" data-testid="skill-panel-my-detail-enable-label" data-variant={selectedSkill.enabled !== false ? 'enabled' : 'disabled'}>
                        {selectedSkill.enabled !== false ? t('skills.enable') : t('skills.disable')}
                      </span>
                    </div>
                    {/* 去试试 */}
                    <button
                      onClick={selectedSkill.enabled !== false ? () => handleGoToChat(selectedSkill.name, selectedSkill.skill_type) : undefined}
                      disabled={selectedSkill.enabled === false}
                      className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap disabled:opacity-40 disabled:cursor-not-allowed"
                      style={{ height: '32px', padding: '0 24px' }}
                      data-testid="skill-panel-my-detail-go-try-btn"
                    >
                      {t('skills.actions.goTry')}
                    </button>
                    {/* 发布 */}
                    <button
                      onClick={() => {
                        if (getStoredOAuthToken()) {
                          setPublishSkillName(selectedSkill.display_name || selectedSkill.name);
                          setPublishVersion(selectedSkill.version || "");
                          setPublishDisplayName(selectedSkill.display_name || selectedSkill.name);
                          setPublishVersionDesc("");
                          setPublishDrawerOpen(true);
                        } else {
                          setOauthLoginOpen(true);
                        }
                      }}
                      className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
                      style={{ height: '32px', padding: '0 24px' }}
                      data-testid="skill-panel-my-detail-publish-btn"
                    >
                      {t('skills.actions.publish')}
                    </button>
                  </div>
                </div>

                {/* 基本信息 */}
                <div className="mb-6" data-testid="skill-panel-my-detail-basic-info">
                  <div className="text-sm font-semibold text-text mb-2">
                    {t('skills.detail.basicInfo')}
                  </div>
                  <div className="text-sm text-text-muted">
                    {selectedSkill.description || t('skills.noDescription')}
                  </div>
                </div>

                {/* 版本管理 */}
                <div className="mb-6">
                  <div className="flex items-center gap-2 mb-2">
                    <div className="text-sm font-semibold text-text">
                      {t('skills.detail.versionManage')}
                    </div>
                    <button
                      onClick={() => fetchSkillVersions(selectedSkill.name)}
                      disabled={versionsLoadState === 'loading'}
                      className="flex items-center justify-center w-5 h-5 text-text-muted hover:text-text disabled:opacity-50"
                      title={t('common.refresh')}
                      data-testid="skill-panel-my-detail-versions-refresh-btn"
                    >
                      <svg className={`w-4 h-4 ${versionsLoadState === 'loading' ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.582m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                      </svg>
                    </button>
                  </div>
                  {versionsLoadState === 'loading' ? (
                    <div className="text-sm text-text-muted" data-testid="skill-panel-my-detail-versions-state" data-variant="loading">{t('common.loading')}</div>
                  ) : versionsLoadState === 'error' ? (
                    <div className="text-sm text-text-muted" data-testid="skill-panel-my-detail-versions-state" data-variant="error">{t('skills.detail.versionsLoadFailed')}</div>
                  ) : skillVersions.length === 0 ? (
                    <div className="text-sm text-text-muted" data-testid="skill-panel-my-detail-versions-state" data-variant="empty">
                      {selectedSkill.version ? `v${selectedSkill.version}` : (t('skills.detail.noVersions'))}
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 flex-wrap">
                      <select
                        value={selectedSkill.version || skillVersionsDefault || ''}
                        onChange={(e) => {
                          const ver = e.target.value;
                          if (ver) fetchSkillDetail(selectedSkill.name, ver);
                        }}
                        className="appearance-none rounded-[6px] border border-border bg-panel text-sm text-text outline-none focus:outline-none focus:ring-0 focus:border-border"
                        style={{ width: '360px', height: '28px', paddingLeft: '12px', paddingRight: '12px' }}
                        data-testid="skill-panel-my-detail-versions-select"
                      >
                        {buildSkillVersionOptions(skillVersions, {
                          defaultSuffix: ` (${t('skills.detail.defaultVersion')})`,
                          unavailableSuffix: ` (${t('skills.detail.unavailableVersion')})`,
                        }).map((option) => (
                          <option key={option.version} value={option.version} disabled={option.disabled}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                      {selectedSkill.has_evolutions ? (
                        <button
                          onClick={() => handleRebuild(selectedSkill.name, selectedSkill.version || null)}
                          disabled={rebuildLoading}
                          className="flex items-center justify-center rounded-[6px] text-xs text-text-muted border border-border hover:bg-secondary whitespace-nowrap disabled:opacity-50"
                          style={{ height: '28px', padding: '0 12px' }}
                        >
                          {rebuildLoading ? (t('common.processing')) : (t('skills.actions.rebuild'))}
                        </button>
                      ) : null}
                    </div>
                  )}
                </div>

                {/* 三个页签 */}
                <div className="flex-1 flex flex-col min-h-0">
                  <div className="flex items-center mb-4 flex-shrink-0">
                    <div className="flex items-center gap-8 flex-1 border-b border-border">
                      <button
                        onClick={() => setDetailTab("content")}
                        className={`pb-2 text-sm ${
                          detailTab === "content"
                            ? "text-text font-semibold border-b-2 border-text"
                            : "text-text-muted hover:text-text"
                        }`}
                        data-testid="skill-panel-my-detail-tab"
                        data-variant="content"
                      >
                        {t('skills.detail.tabs.contentDetail')}
                      </button>
                      <button
                        onClick={() => {
                          setDetailTab("files");
                          fetchSkillFiles(selectedSkill.name);
                        }}
                        className={`pb-2 text-sm ${
                          detailTab === "files"
                            ? "text-text font-semibold border-b-2 border-text"
                            : "text-text-muted hover:text-text"
                        }`}
                        data-testid="skill-panel-my-detail-tab"
                        data-variant="files"
                      >
                        {t('skills.detail.tabs.filePreview')}
                      </button>
                      {selectedSkill.has_evolutions ? (
                        <button
                          onClick={() => setDetailTab("experience")}
                          className={`pb-2 text-sm ${
                            detailTab === "experience"
                              ? "text-text font-semibold border-b-2 border-text"
                              : "text-text-muted hover:text-text"
                          }`}
                          data-testid="skill-panel-my-detail-tab"
                          data-variant="experience"
                        >
                          {t('skills.detail.tabs.skillExperience')}
                        </button>
                      ) : null}
                    </div>

                    {/* 合成新版本按钮（仅技能经验页签时显示） */}
                    {detailTab === "experience" && selectedSkill.has_evolutions ? (
                      <button
                        onClick={() => handleRebuild(selectedSkill.name, selectedSkill.version || null)}
                        disabled={rebuildLoading}
                        onMouseEnter={(e) => {
                          const rect = e.currentTarget.getBoundingClientRect();
                          setSynthesizeTooltip({ left: rect.left + rect.width / 2, top: rect.top });
                        }}
                        onMouseLeave={() => setSynthesizeTooltip(null)}
                        className="mb-1 flex items-center justify-center rounded-[16px] text-xs font-medium text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap disabled:opacity-50"
                        style={{ width: '118px', height: '32px' }}
                      >
                        {rebuildLoading ? (t('common.processing')) : (t('skills.actions.synthesizeNewVersion'))}
                      </button>
                    ) : null}
                  </div>

                  {/* 内容详情 */}
                  {detailTab === "content" && (
                    <div className="flex-1 min-h-0 overflow-y-auto text-sm text-text bg-secondary border border-border rounded-md p-3">
                      {selectedSkill.content ? (
                        <MarkdownRenderer content={detailContent} className="chat-text chat-markdown" />
                      ) : (
                        t('skills.noContent')
                      )}
                    </div>
                  )}

                  {/* 文件预览 */}
                  {detailTab === "files" && (
                    <div className="flex-1 min-h-0 grid grid-cols-[minmax(0,3fr)_minmax(0,7fr)] gap-3">
                      <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0">
                        <div className="px-3 py-2 bg-secondary/30 border-b border-border flex items-center justify-between">
                          <span className="text-sm font-medium text-text">{t('skills.detail.fileTree')}</span>
                          <div className="flex items-center gap-2">
                            <button type="button" onClick={() => selectedSkill && fetchSkillFiles(selectedSkill.name)} className="text-xs text-text-muted hover:text-text px-2 py-0.5 rounded border border-border hover:bg-secondary">{t('common.refresh')}</button>
                          </div>
                        </div>
                        <div className="flex-1 overflow-auto p-2">
                          {filesLoadState === 'loading' ? (
                            <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('common.loading')}</div>
                          ) : filesLoadState === 'error' ? (
                            <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('skills.detail.filesLoadFailed')}</div>
                          ) : skillFiles.length === 0 ? (
                            <div className="h-full flex items-center justify-center text-sm text-text-muted">{t('skills.detail.noFiles')}</div>
                          ) : (
                            <div className="space-y-0.5">{skillFileTree.children.map(child => renderFileTree(child, 0))}</div>
                          )}
                        </div>
                      </div>
                      <div className="rounded-xl border border-border bg-card/70 backdrop-blur-sm overflow-hidden shadow-sm flex flex-col min-h-0">
                        {filePreview ? (
                          <>
                            <div className="px-3 py-2 bg-secondary/30 border-b border-border flex items-center justify-between flex-shrink-0">
                              <span className="text-sm font-medium text-text truncate">{filePreview.path.split('/').pop()}</span>
                            </div>
                            {filePreview.content != null ? (
                              /\.md$/i.test(filePreview.path) ? (
                                <div className="flex-1 min-h-0 overflow-auto p-3 text-sm text-text">
                                  <MarkdownRenderer
                                    content={filePreview.content}
                                    className="chat-text chat-markdown"
                                  />
                                </div>
                              ) : (
                                <pre className="flex-1 min-h-0 overflow-auto text-xs text-text p-3 whitespace-pre-wrap font-mono">{filePreview.content}</pre>
                              )
                            ) : /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(filePreview.path) ? (
                              <div className="flex-1 min-h-0 overflow-auto flex items-center justify-center p-3">
                                <img src={filePreview.download_url || `/file-api/raw-file?path=${encodeURIComponent(((selectedSkill as SkillDetail)?.file_path || '').replace(/[\\/][^\\/]+$/, '') + '/' + filePreview.path)}`} alt={filePreview.path.split('/').pop() || ''} className="max-w-full max-h-full object-contain" />
                              </div>
                            ) : (
                              <div className="flex-1 flex items-center justify-center text-sm text-text-muted">{filePreview.download_url ? (t('skills.detail.binaryFileDownload')) : (t('skills.detail.noPreview'))}</div>
                            )}
                          </>
                        ) : (
                          <>
                            <div className="px-3 py-2 bg-secondary/30 border-b border-border flex items-center gap-3 flex-shrink-0">
                              <span className="h-8 w-8 rounded-lg border border-border bg-card flex items-center justify-center text-text-muted flex-shrink-0">
                                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} className="h-6 w-6"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" /></svg>
                              </span>
                              <div>
                                <h4 className="text-sm font-medium text-text">{t('skills.detail.contentPreview')}</h4>
                                <p className="text-xs text-text-muted mt-0.5">{t('skills.detail.selectFileToPreview')}</p>
                              </div>
                            </div>
                            <div className="flex-1 min-h-0 flex items-center justify-center">
                              <div className="text-center text-text-muted"><div className="mb-2 text-sm">{t('skills.detail.selectFileToPreview')}</div></div>
                            </div>
                          </>
                        )}
                      </div>
                    </div>
                  )}

                  {/* 技能经验 */}
                  {detailTab === "experience" && selectedSkill.has_evolutions && (
                    <div className="flex-1 min-h-0 overflow-y-auto">
                      {evolutionMessage && (
                        <div
                          className={`mb-3 px-3 py-2 rounded-md text-sm ${
                            evolutionMessageType === "error"
                              ? "bg-secondary text-danger"
                              : "bg-secondary text-text"
                          }`}
                        >
                          {evolutionMessage}
                        </div>
                      )}

                      {evolutionFormatError && (
                        <div className="mb-3 px-3 py-2 rounded-md bg-secondary text-sm text-danger">
                          {evolutionFormatError}
                        </div>
                      )}

                      {evolutionListState === "loading" && (
                        <div className="flex items-center justify-center text-text-muted">{t('common.loading')}</div>
                      )}
                      {evolutionListState === "error" && (
                        <div className="text-sm text-text-muted">
                          {t('skills.evolution.errors.loadFailed')}
                        </div>
                      )}
                      {evolutionListState === "success" && !evolutionFormatError && sortedEvolutionEntries.length === 0 && (
                        <div className="text-sm text-text-muted">
                          {t('skills.evolution.empty')}
                        </div>
                      )}

                      {evolutionListState === "success" && !evolutionFormatError && sortedEvolutionEntries.length > 0 && (
                        <div className="space-y-4">
                          {sortedEvolutionEntries.map((entry) => (
                            <div
                              key={entry.id}
                              className="border border-border py-4 px-4 bg-[var(--color-skill-evolution-card-surface)]"
                              style={{ borderRadius: '8px' }}
                            >
                              <div className="flex items-start justify-between gap-3">
                              <div className="min-w-0 text-xs space-y-1 w-[90%]">
                                <div className="grid grid-cols-3 gap-4">
                                  <div>
                                    <span className="text-text-muted">{t('skills.evolution.fields.section')}:</span>
                                    <span className="ml-1 text-text">{entry.change?.section || "-"}</span>
                                  </div>
                                  <div>
                                    <span className="text-text-muted">{t('skills.evolution.fields.target')}:</span>
                                    <span className="ml-1 text-text">{entry.change?.target || "-"}</span>
                                  </div>
                                  <div>
                                    <span className="text-text-muted">{t('skills.evolution.fields.timestamp')}:</span>
                                    <span className="ml-1 text-text">
                                      {entry.timestamp ? new Date(entry.timestamp).toLocaleString(i18n.language) : "-"}
                                    </span>
                                  </div>
                                </div>
                              </div>
                                <button
                                  type="button"
                                  onClick={() => handleEvolutionDeleteEntry(entry.id)}
                                  className="w-7 h-7 flex items-center justify-center rounded-lg hover:opacity-80 text-text"
                                  title={t('skills.evolution.actions.delete')}
                                >
                                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                                  </svg>
                                </button>
                              </div>

                              <div className="mt-3">
                                <textarea
                                  value={entry.change?.content || ""}
                                  onChange={(event) => handleEvolutionContentChange(entry.id, event.target.value)}
                                  className="w-full min-h-28 px-3 py-2 rounded-md bg-card border border-border text-sm text-text placeholder:text-text-muted"
                                />
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <>
                {listState === "success" && mySkillsFiltered.length === 0 ? (
                  <div className="mt-4 text-sm text-text-muted">
                    {mySkillsSubTab === "disabled" ? t('skills.noDisabledSkills') :
                     mySkillsSubTab === "enabled" ? t('skills.noEnabledSkills') :
                     t('skills.noMatches')}
                  </div>
                ) : null}
                {listState !== "success" || mySkillsFiltered.length > 0 ? (
                <div className="card-grid-auto flex-1 min-h-0 overflow-y-auto" style={{ paddingTop: '16px' }}>
                    {listState === "loading" && (
                      <div className="col-span-3 flex items-center justify-center h-full text-text-muted" data-testid="skill-panel-my-list-loading">{t('common.loading')}</div>
                    )}
                    {listState === "error" && (
                      <div className="col-span-3 text-sm text-text-muted" data-testid="skill-panel-my-list-error">
                        {t('skills.listError')}
                      </div>
                    )}
                    {listState === "success" &&
                    mySkillsFiltered.map((skill) => {
                      const avatar = getSkillAvatar(skill.name);
                      const displayName = skill.display_name || skill.name;
                      const isDisabled = skill.enabled === false;
                      const isToggling = actionTarget === `toggle:${skill.name}`;
                      const isPackage = isSkillPackage(skill);
                      const isMenuOpen = openMenuSkillName === skill.name;
                      const listKey = skill.path || `${skill.source || "local"}:${skill.name}`;
                      return (
                        <div
                          key={listKey}
                          onClick={() => handleOpenSkill(skill.name)}
                          className={CARD_CLASS}
                          data-testid="skill-card"
                          data-variant={listKey}
                        >
                          {/* 上盒子：头像 + 名称/演进 + 来源 + 悬浮按钮 */}
                          <div className="flex items-center gap-3 min-w-0">
                            <div className={`w-12 h-12 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
                              {avatar.firstChar}
                            </div>
                            <div className="min-w-0 flex-1 h-full flex flex-col justify-between">
                              <div className="flex items-center gap-1.5">
                                <span className="text-sm font-semibold text-text-strong truncate leading-5" data-testid="skill-card-name">
                                  {displayName}
                                </span>
                                {skill.has_evolutions ? (
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      handleOpenSkill(skill.name);
                                      setDetailTab("experience");
                                    }}
                                    className="relative shrink-0 w-5 h-5 flex items-center justify-center text-text-muted hover:text-text"
                                    title={t('skills.actions.viewEvolution')}
                                    data-testid="skill-panel-my-skill-card-evolution-btn"
                                  >
                                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" className="lucide lucide-bell-dot-icon lucide-bell-dot"><path d="M10.268 21a2 2 0 0 0 3.464 0"/><path d="M11.68 2.009A6 6 0 0 0 6 8c0 4.499-1.411 5.956-2.738 7.326A1 1 0 0 0 4 17h16a1 1 0 0 0 .74-1.673c-.824-.85-1.678-1.731-2.21-3.348"/><circle cx="18" cy="5" r="3"/></svg>
                                  </button>
                                ) : null}
                              </div>
                              <div className="mt-1 flex items-center gap-1.5 flex-wrap">
                                <SkillTypeBadge skillType={skill.skill_type} withTruncate />
                                {activeTab === "my" ? (
                                  <span className="px-2 h-5 inline-flex items-center rounded bg-secondary border border-border truncate text-xs text-text-muted">
                                    {t('skills.publishFilter.unpublished')}
                                  </span>
                                ) : null}
                              </div>
                            </div>
                            {/* 悬浮按钮 + 始终显示的启用开关 */}
                            <div className="flex items-center gap-1.5 shrink-0" onClick={(e) => e.stopPropagation()}>
                              <div className="flex items-center gap-1.5 opacity-0 group-hover:opacity-100 transition-opacity">
                                <div className="relative">
                                  <button
                                    type="button"
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setOpenMenuSkillName(isMenuOpen ? null : skill.name);
                                    }}
                                    className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text-muted hover:text-text"
                                    data-testid="skill-panel-my-skill-card-menu"
                                  >
                                    <MoreIcon aria-hidden />
                                  </button>
                                  {isMenuOpen ? (
                                    <>
                                      <div className="fixed inset-0 z-40" onClick={(e) => { e.stopPropagation(); setOpenMenuSkillName(null); }} />
                                      <div className="absolute right-0 top-full mt-1 z-50 min-w-[160px] rounded-lg border border-border bg-panel shadow-lg py-1">
                                        {!isPackage ? (
                                          <button
                                            onClick={isDisabled ? undefined : (e: React.MouseEvent) => {
                                              e.stopPropagation();
                                              setOpenMenuSkillName(null);
                                              handleEditSkill(skill.name, skill.skill_type);
                                            }}
                                            disabled={isDisabled}
                                            className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary disabled:opacity-40 disabled:cursor-not-allowed"
                                            data-testid="skill-panel-my-skill-card-menu-edit"
                                          >
                                            {t('skills.actions.edit')}
                                          </button>
                                        ) : null}
                                        <button
                                          onClick={(e) => {
                                            e.stopPropagation();
                                            setOpenMenuSkillName(null);
                                            const plugin = installedSkillMap.get(skill.name);
                                            handleUninstall(plugin?.plugin_name || skill.name);
                                          }}
                                          className="flex items-center w-full px-3 py-2 text-sm text-left text-text hover:bg-secondary"
                                          data-testid="skill-panel-my-skill-card-menu-uninstall"
                                        >
                                          {t('skills.actions.uninstall')}
                                        </button>
                                      </div>
                                    </>
                                  ) : null}
                                </div>
                                <button
                                  type="button"
                                  onClick={isDisabled ? undefined : (e: React.MouseEvent) => {
                                    e.stopPropagation();
                                    handleGoToChat(skill.name, skill.skill_type);
                                  }}
                                  disabled={isDisabled}
                                  onMouseEnter={(e) => {
                                    if (isDisabled) return;
                                    const rect = e.currentTarget.getBoundingClientRect();
                                    setGoTryTooltip({ left: rect.left + rect.width / 2, top: rect.top });
                                  }}
                                  onMouseLeave={() => setGoTryTooltip(null)}
                                  className="w-7 h-7 flex items-center justify-center rounded-md hover:bg-secondary text-text-muted hover:text-text disabled:opacity-40 disabled:cursor-not-allowed"
                                  data-testid="skill-panel-my-skill-card-go-try-btn"
                                >
                                  <NewConversationIcon aria-hidden width="16" height="16" />
                                </button>
                              </div>
                              <Switch
                                checked={!isDisabled}
                                onChange={() => toggleSkillDisabled(skill.name)}
                                disabled={isToggling}
                              />
                            </div>
                          </div>
                          {/* 下盒子：描述 */}
                          <div className="text-xs text-text-muted mt-[18px] line-clamp-2 leading-[22px]">
                            {skill.description || t('skills.noDescription')}
                          </div>
                        </div>
                      );
                    })}
                </div>
                ) : null}
              </>
            )}
          </>
        ) : null}
      </div>
      <SourceManagerModal
        open={sourceModalOpen}
        sessionId={sessionId}
        onClose={() => setSourceModalOpen(false)}
        onNavigateToSettings={() => {
          setSourceModalOpen(false);
          onNavigateToSettings?.();
        }}
      />
      <SkillNetSearchModal
        open={skillNetModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        installedSkillOrigins={installedSkillOrigins}
        onClose={() => setSkillNetModalOpen(false)}
        onInstalled={async () => {
          await fetchSkills();
        }}
        onNavigateToSettings={() => {
          setSkillNetModalOpen(false);
          onNavigateToSettings?.();
        }}
      />
      <ClawHubSearchModal
        open={clawHubModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        installedSkillOrigins={installedSkillOrigins}
        onClose={() => setClawHubModalOpen(false)}
        onInstalled={async () => {
          await fetchSkills();
        }}
      />
      <TeamSkillsHubModal
        open={teamSkillsHubModalOpen}
        sessionId={sessionId}
        installedSkillNames={installedSkillNames}
        onClose={() => setTeamSkillsHubModalOpen(false)}
        onInstalled={async () => {
          await fetchSkills();
        }}
      />
      {/* 上传技能弹窗 */}
      {uploadSkillModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <button
            type="button"
            className="absolute inset-0 bg-black/60"
            onClick={() => { setUploadSkillModalOpen(false); setUploadSkillPath(""); setUploadSkillFile(null); }}
            aria-label={t('skills.uploadSkillModal.cancel')}
          />
          <div
            className="relative overflow-hidden rounded-[8px] border border-border bg-card shadow-2xl animate-rise flex flex-col"
            style={{ width: '550px' }}
            data-testid="skill-panel-upload-skill-modal"
          >
            {/* 头部 */}
            <div className="flex items-center justify-between gap-3 px-5 py-3 bg-panel">
              <span className="text-lg font-semibold text-text-strong" data-testid="skill-panel-upload-skill-modal-title">
                {t('skills.uploadSkillModal.title')}
              </span>
              <ModalCloseButton
                onClick={() => { setUploadSkillModalOpen(false); setUploadSkillPath(""); setUploadSkillFile(null); }}
                label={t('skills.uploadSkillModal.cancel')}
                testId="skill-panel-upload-skill-modal-close-btn"
              />
            </div>
            {/* 提示行 */}
            <div className="px-5 pt-3">
              <div
                className="flex items-start gap-1.5 rounded-[8px] px-3 py-2 text-xs text-text bg-[var(--color-skill-notice-surface)]"
                style={{ width: '502px' }}
              >
                <TipIcon className="w-3.5 h-3.5 shrink-0 mt-0.5" />
                <span className="leading-4">{t('skills.uploadSkillModal.notice')}</span>
              </div>
            </div>
            {/* 文件上传拖动框 */}
            <div className="px-5 pt-3 pb-5">
              <label
                onDragOver={(e) => { e.preventDefault(); }}
                onDrop={(e) => {
                  e.preventDefault();
                  const file = e.dataTransfer.files[0];
                  if (file) {
                    setUploadSkillPath(file.name);
                    setUploadSkillFile(file);
                  }
                }}
                className="flex flex-col items-center justify-center gap-2 rounded-[12px] border border-dashed border-border cursor-pointer bg-[var(--color-skill-dropzone-surface)] hover:bg-[var(--color-skill-dropzone-hover-surface)]"
                style={{ width: '502px', height: '160px' }}
              >
                <UpFileIcon className="w-10 h-10 text-text-muted" />
                <span className="text-sm text-text-muted">
                  {uploadSkillPath.trim()
                    ? uploadSkillPath
                    : t('skills.uploadSkillModal.dropHint')}
                </span>
                <input
                  type="file"
                  accept=".zip"
                  className="hidden"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) {
                      setUploadSkillPath(file.name);
                      setUploadSkillFile(file);
                    }
                  }}
                />
              </label>
            </div>
            {/* 底部按钮 */}
            <div className="flex items-center justify-end gap-3 px-5 py-3 bg-panel">
              <button
                type="button"
                onClick={() => { setUploadSkillModalOpen(false); setUploadSkillPath(""); setUploadSkillFile(null); }}
                className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
                style={{ height: '32px', padding: '0 32px' }}
              >
                {t('skills.uploadSkillModal.cancel')}
              </button>
              <button
                type="button"
                disabled={!uploadSkillFile || actionTarget === "import_local"}
                onClick={() => {
                  const file = uploadSkillFile;
                  setUploadSkillModalOpen(false);
                  setUploadSkillPath("");
                  setUploadSkillFile(null);
                  if (file) handleSkillUpload(file);
                }}
                className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
                  !uploadSkillFile || actionTarget === "import_local"
                    ? 'bg-secondary text-text-muted cursor-not-allowed'
                    : 'text-text-inverse bg-control-emphasis hover:opacity-80'
                }`}
                style={{ height: '32px', padding: '0 32px' }}
              >
                {t('skills.uploadSkillModal.confirm')}
              </button>
            </div>
          </div>
        </div>
      )}
      {/* 知识转技能弹窗 */}
      {docToSkillModalOpen && (() => {
        const isDocConfirmDisabled = docToSkillSource === "local" ? !docToSkillFile : !docToSkillLink.trim();
        return (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <button
            type="button"
            className="absolute inset-0 bg-black/60"
            onClick={() => { setDocToSkillModalOpen(false); setDocToSkillPath(""); setDocToSkillFile(null); setDocToSkillLink(""); setDocToSkillDesc(""); }}
            aria-label={t('skills.docToSkillModal.cancel')}
          />
          <div
            className="relative overflow-hidden rounded-[8px] border border-border bg-card shadow-2xl animate-rise flex flex-col"
            style={{ width: '550px' }}
          >
            {/* 头部 */}
            <div className="flex items-center justify-between gap-3 px-5 pt-3 pb-0 bg-panel">
              <span className="text-lg font-semibold text-text-strong">
                {t('skills.docToSkillModal.title')}
              </span>
              <ModalCloseButton
                onClick={() => { setDocToSkillModalOpen(false); setDocToSkillPath(""); setDocToSkillFile(null); setDocToSkillLink(""); setDocToSkillDesc(""); }}
                label={t('skills.docToSkillModal.cancel')}
              />
            </div>
            {/* 副标题 */}
            <div className="px-5">
              <span className="text-xs text-text-muted">{t('skills.docToSkillModal.subtitle')}</span>
            </div>
            {/* 来源 */}
            <div className="px-5 pt-4">
              <span className="block text-sm font-medium text-text mb-2">
                {t('skills.docToSkillModal.sourceLabel')}
              </span>
              <div className="flex items-center gap-4">
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    checked={docToSkillSource === "local"}
                    onChange={() => setDocToSkillSource("local")}
                    className="w-3.5 h-3.5 accent-[var(--color-chat-accent)]"
                  />
                  <span className="text-sm text-text">{t('skills.docToSkillModal.sourceLocal')}</span>
                </label>
                <label className="flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="radio"
                    checked={docToSkillSource === "link"}
                    onChange={() => setDocToSkillSource("link")}
                    className="w-3.5 h-3.5 accent-[var(--color-chat-accent)]"
                  />
                  <span className="text-sm text-text">{t('skills.docToSkillModal.sourceLink')}</span>
                </label>
              </div>
            </div>
            {/* 本地上传 */}
            {docToSkillSource === "local" && (
              <div className="px-5 pt-3">
                <label
                  onDragOver={(e) => { e.preventDefault(); }}
                  onDrop={(e) => {
                    e.preventDefault();
                    const file = e.dataTransfer.files[0];
                    if (file) {
                      setDocToSkillPath(file.name);
                      setDocToSkillFile(file);
                    }
                  }}
                  className="flex flex-col items-center justify-center gap-2 rounded-[12px] border border-dashed border-border cursor-pointer bg-[var(--color-skill-dropzone-surface)] hover:bg-[var(--color-skill-dropzone-hover-surface)]"
                  style={{ width: '502px', height: '160px' }}
                >
                  <svg className="w-10 h-10 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" />
                  </svg>
                  <span className="text-sm text-text-muted whitespace-pre-line text-center">
                    {docToSkillPath.trim()
                      ? docToSkillPath
                      : t('skills.docToSkillModal.dropHint')}
                  </span>
                  <input
                    type="file"
                    className="hidden"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) {
                        setDocToSkillPath(file.name);
                        setDocToSkillFile(file);
                      }
                    }}
                  />
                </label>
              </div>
            )}
            {/* 链接 */}
            {docToSkillSource === "link" && (
              <div className="px-5 pt-3">
                <div className="flex items-center gap-1.5 mb-2">
                  <span className="text-sm font-medium text-text">
                    {t('skills.docToSkillModal.linkLabel')}
                  </span>
                  <span
                    onMouseEnter={(e) => {
                      const rect = e.currentTarget.getBoundingClientRect();
                      setDocToSkillTooltip({ left: rect.left + rect.width / 2, top: rect.top });
                    }}
                    onMouseLeave={() => setDocToSkillTooltip(null)}
                    className="w-4 h-4 flex items-center justify-center text-text-muted cursor-help"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                      <circle cx="12" cy="12" r="10" />
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3M12 17h.01" />
                    </svg>
                  </span>
                </div>
                <input
                  type="text"
                  value={docToSkillLink}
                  onChange={(e) => setDocToSkillLink(e.target.value)}
                  placeholder={t('skills.docToSkillModal.linkPlaceholder')}
                  className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text"
                  style={{ maxWidth: '502px' }}
                />
              </div>
            )}
            {/* 技能描述 */}
            <div className="px-5 pt-4">
              <span className="block text-sm font-medium text-text mb-1.5">
                {t('skills.docToSkillModal.descLabel')}
              </span>
              <input
                type="text"
                value={docToSkillDesc}
                onChange={(e) => setDocToSkillDesc(e.target.value)}
                placeholder={t('skills.docToSkillModal.descPlaceholder')}
                className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text"
                style={{ maxWidth: '502px' }}
              />
            </div>
            {/* 底部按钮 */}
            <div className="flex items-center justify-end gap-3 px-5 pt-4 pb-4 bg-panel">
              <button
                type="button"
                onClick={() => { setDocToSkillModalOpen(false); setDocToSkillPath(""); setDocToSkillFile(null); setDocToSkillLink(""); setDocToSkillDesc(""); }}
                className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
                style={{ height: '32px', padding: '0 32px' }}
              >
                {t('skills.docToSkillModal.cancel')}
              </button>
              <button
                type="button"
                disabled={isDocConfirmDisabled}
                onClick={() => {
                  const file = docToSkillFile;
                  const link = docToSkillLink;
                  const desc = docToSkillDesc;
                  setDocToSkillModalOpen(false);
                  setDocToSkillPath("");
                  setDocToSkillFile(null);
                  setDocToSkillLink("");
                  setDocToSkillDesc("");
                  handleCreateFromKnowledge({
                    file: docToSkillSource === "local" ? file : null,
                    link: docToSkillSource === "link" ? link : undefined,
                    skillDescription: desc,
                  });
                }}
                className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
                  isDocConfirmDisabled
                    ? 'bg-secondary text-text-muted cursor-not-allowed'
                    : 'text-text-inverse bg-control-emphasis hover:opacity-80'
                }`}
                style={{ height: '32px', padding: '0 32px' }}
              >
                {t('skills.docToSkillModal.confirm')}
              </button>
            </div>
          </div>
        </div>
        );
      })()}
      {synthesizeTooltip && (
        <TopAnchorTooltip pos={synthesizeTooltip} text={t('skills.actions.synthesizeTooltip')} />
      )}
      {goTryTooltip && (
        <TopAnchorTooltip pos={goTryTooltip} text={t('skills.actions.goTry')} />
      )}
      {docToSkillTooltip && (
        <TopAnchorTooltip pos={docToSkillTooltip} text={t('skills.docToSkillModal.linkTooltip')} />
      )}
      {/* 发布技能右侧弹窗 */}
      {publishDrawerOpen && selectedSkill && getStoredOAuthToken() && (() => {
        const isPublishDisabled = !publishSkillName || !publishVersion || !publishDisplayName;
        return (
        <>
          <div
            className="fixed inset-0 z-[9998] bg-black/30"
            onClick={() => setPublishDrawerOpen(false)}
          />
          <div
            className="fixed top-0 right-0 bottom-0 z-[9999] bg-panel border-l border-border shadow-2xl flex flex-col"
            style={{ width: '550px' }}
            data-testid="skill-panel-publish-drawer"
          >
            {/* 头部（无分割线） */}
            <div className="flex items-center justify-between px-6 pt-4 pb-2 flex-shrink-0">
              <span className="text-base font-semibold text-text-strong" data-testid="skill-panel-publish-drawer-title">
                {t('skills.publishForm.title')}
              </span>
              <ModalCloseButton onClick={() => setPublishDrawerOpen(false)} label={t('skills.publishForm.cancel')} testId="skill-panel-publish-drawer-close-btn" />
            </div>
            {/* 提示行（标题下方，可关闭） */}
            {publishNoticeVisible && (
              <div
                className="mx-6 mb-2 flex items-center gap-1.5 rounded-[6px] px-3 text-xs text-text flex-shrink-0 bg-[var(--color-skill-notice-surface)]"
                style={{ height: '34px' }}
              >
                <TipIcon className="w-3.5 h-3.5 shrink-0" />
                <span>{t('skills.publishForm.noticeText')}</span>
                <a
                  href={t('skills.publishForm.noticeUrl')}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-0.5 text-chat-accent hover:underline"
                >
                  {t('skills.publishForm.noticeView')}
                  <LinkIcon className="w-3 h-3" />
                </a>
                <button
                  type="button"
                  onClick={() => setPublishNoticeVisible(false)}
                  className="ml-auto w-5 h-5 flex items-center justify-center rounded hover:bg-accent/10 text-text-muted hover:text-text"
                >
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            )}
            {/* 发布错误提示 */}
            {publishError && (
              <div className="mx-6 mt-2 px-3 py-2 rounded-[6px] border border-danger bg-danger/10 text-xs text-danger">
                {publishError}
              </div>
            )}
            {/* 表单内容 */}
            <div className="flex-1 overflow-y-auto px-6 py-2 space-y-4">
              {/* 技能名 */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.skillName')} <span className="text-danger">*</span>
                  <FormFieldTooltip text={t('skills.publishForm.skillNameTooltip')} />
                </label>
                <input
                  type="text"
                  value={publishSkillName}
                  onChange={(e) => {
                    setPublishSkillName(e.target.value);
                    const err = validatePublishSkillName(e.target.value);
                    setPublishFieldErrors(prev => ({ ...prev, skillName: err || '' }));
                  }}
                  placeholder="my-demo-skill"
                  className={`w-full px-3 py-2 rounded-[6px] border bg-panel text-sm text-text ${publishFieldErrors.skillName ? 'border-danger' : 'border-border'}`}
                />
                {publishFieldErrors.skillName && (
                  <p className="mt-1 text-xs text-danger">{publishFieldErrors.skillName}</p>
                )}
              </div>
              {/* 版本号 */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.version')} <span className="text-danger">*</span>
                </label>
                <input
                  type="text"
                  value={publishVersion}
                  onChange={(e) => {
                    setPublishVersion(e.target.value);
                    const err = validatePublishVersion(e.target.value);
                    setPublishFieldErrors(prev => ({ ...prev, version: err || '' }));
                  }}
                  placeholder="1.0.0"
                  className={`w-full px-3 py-2 rounded-[6px] border bg-panel text-sm text-text ${publishFieldErrors.version ? 'border-danger' : 'border-border'}`}
                />
                {publishFieldErrors.version && (
                  <p className="mt-1 text-xs text-danger">{publishFieldErrors.version}</p>
                )}
              </div>
              {/* 显示名 */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.displayName')} <span className="text-danger">*</span>
                  <FormFieldTooltip text={t('skills.publishForm.displayNameTooltip')} />
                </label>
                <input
                  type="text"
                  value={publishDisplayName}
                  onChange={(e) => {
                    setPublishDisplayName(e.target.value);
                    const err = validatePublishDisplayName(e.target.value);
                    setPublishFieldErrors(prev => ({ ...prev, displayName: err || '' }));
                  }}
                  placeholder={t('skills.publishForm.placeholderSelect')}
                  className={`w-full px-3 py-2 rounded-[6px] border bg-panel text-sm text-text ${publishFieldErrors.displayName ? 'border-danger' : 'border-border'}`}
                />
                {publishFieldErrors.displayName && (
                  <p className="mt-1 text-xs text-danger">{publishFieldErrors.displayName}</p>
                )}
              </div>
              {/* 描述（可选） */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.descriptionOptional')}
                  <FormFieldTooltip text={t('skills.publishForm.descriptionTooltip')} />
                </label>
                <textarea
                  defaultValue={selectedSkill.description || ""}
                  placeholder={t('skills.publishForm.placeholderSelect')}
                  className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text min-h-[72px]"
                />
              </div>
              {/* 标签（可选） */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.tagsOptional')}
                  <FormFieldTooltip text={t('skills.publishForm.tagsTooltip')} />
                </label>
                <input
                  type="text"
                  defaultValue={coerceStringList(selectedSkill.tags).join(", ")}
                  className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text"
                />
              </div>
              {/* Skill图标（可选）- 图片上传框 */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.skillIconOptional')}
                </label>
                <div
                  className="flex items-center justify-center rounded-[6px] border border-dashed border-border bg-secondary/30 cursor-pointer hover:bg-secondary/50"
                  style={{ width: '100px', height: '100px' }}
                >
                  <UpImgIcon className="w-8 h-8 text-text-muted" />
                </div>
                <span className="block mt-1.5 text-xs text-text-muted">
                  {t('skills.publishForm.skillIconHint')}
                </span>
              </div>
              {/* SHA-256 校验和 */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.sha256')}
                </label>
                <input
                  type="text"
                  value=""
                  readOnly
                  placeholder={t('skills.publishForm.placeholderSha256')}
                  className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text font-mono opacity-60 cursor-not-allowed"
                />
              </div>
              {/* 版本说明（Swarm Skill，可选） */}
              <div>
                <label className="block text-sm font-medium text-text mb-1.5">
                  {t('skills.publishForm.versionNoteOptional')}
                </label>
                <textarea
                  value={publishVersionDesc}
                  onChange={(e) => setPublishVersionDesc(e.target.value)}
                  className="w-full px-3 py-2 rounded-[6px] border border-border bg-panel text-sm text-text min-h-[72px]"
                />
              </div>
              {/* 强制覆盖 */}
              <div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={publishForce}
                    onChange={(e) => setPublishForce(e.target.checked)}
                    className="cursor-pointer"
                  />
                  <span className="text-sm text-text">
                    {t('skills.publishForm.forceOverwrite')}
                  </span>
                </label>
              </div>
            </div>
            {/* 底部按钮 */}
            <div className="flex items-center justify-end gap-3 px-6 pb-4 pt-2 flex-shrink-0">
              <button
                type="button"
                onClick={() => setPublishDrawerOpen(false)}
                className="flex items-center justify-center rounded-[16px] text-sm text-control-emphasis bg-card border border-control-emphasis hover:bg-secondary/30 whitespace-nowrap"
                style={{ height: '32px', padding: '0 32px' }}
              >
                {t('skills.publishForm.cancel')}
              </button>
              <button
                type="button"
                disabled={isPublishDisabled || actionTarget === "publish"}
                onClick={() => handlePublish()}
                className={`flex items-center justify-center rounded-[16px] text-sm whitespace-nowrap transition-colors ${
                  isPublishDisabled || actionTarget === "publish"
                    ? 'bg-secondary text-text-muted cursor-not-allowed'
                    : 'text-text-inverse bg-control-emphasis hover:opacity-80'
                }`}
                style={{ height: '32px', padding: '0 32px' }}
              >
                {actionTarget === "publish" ? t('common.processing') : t('skills.publishForm.publish')}
              </button>
            </div>
          </div>
        </>
        );
      })()}
      {/* OAuth 登录弹窗 */}
      {oauthLoginOpen && (
        <>
          <div
            className="fixed inset-0 z-[9998] bg-black/30"
            onClick={() => setOauthLoginOpen(false)}
          />
          <div
            className="fixed left-1/2 top-1/2 z-[9999] -translate-x-1/2 -translate-y-1/2 bg-panel rounded-[16px] shadow-2xl border border-border flex flex-col"
            style={{ width: '420px' }}
            data-testid="skill-panel-oauth-login-modal"
          >
            {/* 头部 */}
            <div className="flex items-center justify-between px-6 pt-5 pb-3">
              <span className="text-base font-semibold text-text-strong" data-testid="skill-panel-oauth-login-title">
                {t('skills.oauthLogin.title')}
              </span>
              <ModalCloseButton onClick={() => setOauthLoginOpen(false)} label={t('skills.oauthLogin.title')} testId="skill-panel-oauth-login-close-btn" />
            </div>
            {/* 内容 */}
            <div className="px-6 pb-6 flex flex-col items-center">
              <p className="text-sm text-text-muted text-center mb-6">
                {t('skills.oauthLogin.description')}
              </p>
              {/* GitCode 登录按钮（始终显示，未配置时点击会提示） */}
              <button
                type="button"
                onClick={() => handleOAuthLogin('gitcode')}
                disabled={oauthLoadingProvider === 'gitcode'}
                className="flex items-center justify-center gap-2 rounded-[16px] text-sm whitespace-nowrap transition-colors w-full mb-3 bg-control-emphasis text-control-emphasis-foreground disabled:cursor-not-allowed disabled:opacity-60"
                data-testid="skill-panel-oauth-login-btn"
                data-variant="gitcode"
                style={{ height: '40px' }}
              >
                {oauthLoadingProvider === 'gitcode' ? (
                  <div className="w-4 h-4 border-2 border-control-emphasis-foreground/30 border-t-control-emphasis-foreground rounded-full animate-spin" />
                ) : (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8 6L2 12L8 18M16 6L22 12L16 18" />
                  </svg>
                )}
                {oauthLoadingProvider === 'gitcode' ? t('skills.oauthLogin.loading') : t('skills.oauthLogin.gitcodeLogin')}
              </button>
              {/* GitHub 登录按钮（始终显示，未配置时点击会提示） */}
              <button
                type="button"
                onClick={() => handleOAuthLogin('github')}
                disabled={oauthLoadingProvider === 'github'}
                className="flex items-center justify-center gap-2 rounded-[16px] text-sm whitespace-nowrap transition-colors w-full bg-card text-control-emphasis border border-control-emphasis disabled:cursor-not-allowed disabled:opacity-60"
                data-testid="skill-panel-oauth-login-btn"
                data-variant="github"
                style={{ height: '40px' }}
              >
                {oauthLoadingProvider === 'github' ? (
                  <div className="w-4 h-4 border-2 border-control-emphasis/30 border-t-control-emphasis rounded-full animate-spin" />
                ) : (
                  <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                    <path fillRule="evenodd" clipRule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.009-.868-.014-1.703-2.782.605-3.369-1.342-3.369-1.342-.454-1.155-1.11-1.463-1.11-1.463-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.339-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0112 6.844c.85.004 1.705.115 2.504.337 1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.203 2.398.1 2.651.641.7 1.029 1.595 1.029 2.688 0 3.848-2.338 4.695-4.566 4.943.359.309.678.92.678 1.855 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.02 10.02 0 0022 12.017C22 6.484 17.522 2 12 2z" />
                  </svg>
                )}
                {oauthLoadingProvider === 'github' ? t('skills.oauthLogin.loading') : t('skills.oauthLogin.githubLogin')}
              </button>
              <p className="mt-4 text-xs text-text-muted text-center" data-testid="skill-panel-oauth-login-callback-hint">
                {t('skills.oauthLogin.callbackHint')}
              </p>
              {oauthError && (
                <p className="mt-4 text-xs text-[var(--color-feedback-error)] text-center">
                  {oauthError}
                </p>
              )}
            </div>
          </div>
        </>
      )}
    </div>
      </>
    );
}
