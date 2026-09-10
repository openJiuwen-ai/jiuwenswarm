/**
 * SkillPanel 组件
 *
 * Skills 管理面板
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from 'react-i18next';
import { ChevronRight } from 'lucide-react';
import { webRequest } from "../../services/webClient";
import { SourceManagerModal } from "../../features/SourceManagerModal";
import { SkillNetSearchModal } from "../../features/SkillNetSearchModal";
import { ClawHubSearchModal } from "../../features/ClawHubSearchModal";
import { TeamSkillsHubModal } from "../../features/TeamSkillsHubModal";
import { EnterpriseSkillSourcePanel, fetchEnterpriseSkillSources } from "../../features/EnterpriseSkillSourcePanel";
import { resolveEnterpriseSourceCount } from "../../features/EnterpriseSkillSourcePanel/sourceAvailability";
import { skillPresentationName } from "../../features/EnterpriseSkillSourcePanel/installMetadata";
import { Pagination } from "../common/Pagination";
import { ConfirmDialog } from "../common/ConfirmDialog";
import { OnlineSkillSearchPanel } from "../../features/OnlineSkillSearchPanel";
import { SkillEvolutionModal } from "../../features/SkillEvolutionModal";
import { normalizeSkillNetUrl } from "../../utils/skillNetUrl";
import { buildSkillListParams, LatestSkillListRequest, shouldFetchSkillList } from "./skillListRequest";
import { getSkillAvatar } from "../../utils/skillAvatar";
import { SkillGraphPanel, type SkillGraphPanelHandle } from "../SkillGraphPanel";
import { MarkdownRenderer } from "../MarkdownRenderer";
import { Switch } from "../Switch";
import { isEnterprise } from "../../edition";

/** 刷新会 git pull marketplace，略放宽；普通进页单次 RPC 一般很快。 */
const SKILLS_FETCH_TIMEOUT_REFRESH_MS = 60_000;
const SKILLS_FETCH_TIMEOUT_NORMAL_MS = 30_000;
const SKILL_RETRIEVAL_RUNNING_POLL_MS = 10_000;
const SKILL_RETRIEVAL_IDLE_POLL_MS = 5 * 60_000;
const GRAPH_READING_MIN_VISIBLE_MS = 500;
const MY_SKILLS_PAGE_SIZE_DEFAULT = 20;

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
  /** TeamSkillsHub 安装时落盘的市场卡片描述，供「我的技能」页与搜索页显示一致 */
  market_short_desc?: string;
  /** TeamSkillsHub 安装时的市场展示名 */
  market_display_name?: string;
  /** 是否为内置技能（不允许删除） */
  is_builtin?: boolean;
  /** 是否为内置技能的来源（源码中存在内置版本） */
  is_builtin_source?: boolean;
  /** 本地技能目录是否存在 evolutions.json */
  has_evolutions?: boolean;
  /** 是否启用 */
  enabled?: boolean;
  /** 是否已落盘并登记（企业 DTO） */
  installed?: boolean;
  /** 企业安装类型；prebuilt 由管理员统一管理 */
  source_type?: 'prebuilt' | 'user' | string;
  /** 是否允许当前调用方卸载（企业 DTO） */
  removable?: boolean;
};

type InstalledPluginItem = {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit?: string | null;
  skills: string[];
};

type MarketplaceItem = {
  name: string;
  url: string;
  install_location: string;
  last_updated?: string | null;
};

type SkillDetail = SkillItem & {
  content: string;
  file_path: string;
};

type LoadState = "idle" | "loading" | "success" | "error";

type SkillRetrievalStatus = {
  enabled?: boolean;
  index_exists?: boolean;
  fresh?: boolean;
  installed_count?: number;
  installed_enabled_count?: number;
  indexed_count?: number;
  built_at?: string;
  index_dir?: string;
  build_status?: string;
  build_stage?: string;
  build_message?: string;
  build_error?: string;
  build_progress?: number;
  build_started_at?: string;
  build_finished_at?: string;
  build_elapsed_seconds?: number;
  build_cancel_requested?: boolean;
  build_logs?: SkillRetrievalBuildLog[];
};

type SkillRetrievalBuildLog = {
  time?: string;
  stage?: string;
  status?: string;
  message?: string;
};

type SkillRetrievalTreeResponse = {
  success?: boolean;
  result?: string;
  nodes?: SkillIndexNode[];
  branch_count?: number;
  leaf_count?: number;
  index_dir?: string;
};

type SkillIndexNode = {
  cid: string;
  parent_cid?: string;
  type?: "branch" | "leaf" | string;
  label?: string;
  description?: string;
  select_when?: string;
  dont_select_when?: string;
  source_description?: string;
  worker_id?: string;
  skill_name?: string;
  category?: string;
  keywords?: string[];
  examples?: string[];
};

type SkillIndexTreeNode = SkillIndexNode & {
  children: SkillIndexTreeNode[];
};

interface SkillPanelProps {
  sessionId: string;
  onNavigateToConfig?: () => void;
  /** 当前是否处于激活状态（左边栏选中技能） */
  isActive?: boolean;
}

function getSourceLabel(source: string, t: (key: string) => string, isBuiltinSource?: boolean, sourceType?: string): string {
  if (sourceType === 'prebuilt') return t('skills.enterprisePrebuilt');
  if (sourceType === 'builtin') return t('skills.source.builtin');
  if (isBuiltinSource) return t('skills.source.builtin');
  if (source === "local") return t('skills.source.local');
  if (source === "project") return t('skills.source.project');
  if (source === "builtin") return t('skills.source.builtin');
  if (source === "clawhub") return t('skills.source.clawhub');
  if (source === "skillnet") return t('skills.source.skillnet');
  if (source === "teamskillshub") return t('skills.source.teamskillshub');
  if (source === "swarmskillhub") return t('skills.source.swarmskillhub');
  return source || t('skills.source.unknown');
}

function isAdministratorManagedSkill(skill: SkillItem): boolean {
  // 企业 DTO：removable 是后端给的权威字段（user=true 可卸载/启停，builtin/prebuilt=false）。
  if (typeof skill.removable === 'boolean') return !skill.removable;
  return skill.source_type === 'prebuilt' || skill.source_type === 'builtin';
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

/**
 * 「我的技能」页的展示描述：优先用 TeamSkillsHub 安装时落盘的市场卡片描述
 * （与搜索页 short_desc 一致），缺省回退 SKILL.md 的 description，再缺省走占位文案。
 * 来自 teamskillshub 来源的技能会带 market_short_desc；其余来源不受影响。
 */
function skillDisplayDesc(skill: SkillItem, fallback: string): string {
  return skill.market_short_desc || skill.description || fallback;
}

function buildSkillIndexTree(nodes: SkillIndexNode[]): SkillIndexTreeNode[] {
  const map = new Map<string, SkillIndexTreeNode>();
  nodes.forEach((node) => {
    const cid = String(node.cid || "").trim();
    if (!cid) return;
    map.set(cid, { ...node, cid, children: [] });
  });

  const roots: SkillIndexTreeNode[] = [];
  map.forEach((node) => {
    const parentCid = String(node.parent_cid || "").trim();
    const parent = parentCid ? map.get(parentCid) : undefined;
    if (parent) {
      parent.children.push(node);
    } else {
      roots.push(node);
    }
  });

  const sortNodes = (items: SkillIndexTreeNode[]) => {
    items.sort((a, b) => {
      const aType = a.type === "leaf" ? 1 : 0;
      const bType = b.type === "leaf" ? 1 : 0;
      if (aType !== bType) return aType - bType;
      return getSkillIndexNodeLabel(a).localeCompare(getSkillIndexNodeLabel(b));
    });
    items.forEach((item) => sortNodes(item.children));
  };
  sortNodes(roots);
  return roots;
}

function getSkillIndexNodeLabel(node: SkillIndexNode): string {
  return String(node.label || node.worker_id || node.cid || "").trim() || "node";
}

function getSkillIndexSkillName(node: SkillIndexNode): string {
  return String(node.skill_name || node.worker_id || node.label || "").trim();
}

function getSkillIndexNodeClassName(disabledLeaf: boolean, selected: boolean): string {
  if (disabledLeaf) {
    return selected
      ? "border-zinc-400/40 bg-zinc-500/10 text-text-muted"
      : "border-transparent text-text-muted opacity-75 hover:bg-secondary/50";
  }
  if (selected) {
    return "border-accent/40 bg-accent/10 text-accent";
  }
  return "border-transparent text-text hover:bg-secondary/60";
}

function getSkillIndexNodeBadgeClassName(disabledLeaf: boolean, isLeaf: boolean): string {
  if (disabledLeaf) {
    return "border-zinc-400/25 bg-zinc-500/10 text-text-muted";
  }
  if (isLeaf) {
    return "border-emerald-500/25 bg-emerald-500/10 text-emerald-600";
  }
  return "border-sky-500/25 bg-sky-500/10 text-sky-600";
}

function findSkillIndexNode(nodes: SkillIndexNode[], cid: string | null): SkillIndexNode | null {
  if (!cid) return null;
  return nodes.find((node) => node.cid === cid) || null;
}

type SkillIndexBuildPhaseState = "done" | "active" | "pending" | "failed" | "cancelled";

type SkillIndexBuildPhase = {
  key: string;
  title: string;
  detail: string;
  state: SkillIndexBuildPhaseState;
};

function getSkillIndexBuildStageLabel(
  stage: string | undefined,
  t: (key: string, options?: Record<string, unknown>) => string
): string {
  const key = String(stage || "").trim();
  if (!key) return t('skills.retrieval.buildStageUnknown');
  const known: Record<string, string> = {
    queued: 'queued',
    scan: 'scan',
    llm_check: 'llmCheck',
    build: 'buildTree',
    publish: 'publish',
    reuse: 'reuse',
    success: 'success',
    failed: 'failed',
    timeout: 'timeout',
    llm_config: 'llmConfig',
    cancelled: 'cancelled',
    interrupted: 'interrupted',
  };
  const mapped = known[key];
  return mapped ? t(`skills.retrieval.buildStages.${mapped}`) : key;
}

function getSkillIndexBuildPhaseState(
  phaseKey: string,
  currentStage: string,
  buildStatus: string
): SkillIndexBuildPhaseState {
  const order = ["queued", "scan", "llm_check", "build", "publish", "success"];
  const normalizedStage = order.includes(currentStage)
    ? currentStage
    : currentStage === "llm_config"
    ? "llm_check"
    : ["failed", "timeout", "interrupted", "cancelled"].includes(currentStage)
    ? "build"
    : buildStatus === "success"
    ? "success"
    : "queued";
  const currentIndex = order.indexOf(normalizedStage);
  const phaseIndex = order.indexOf(phaseKey);
  if (buildStatus === "failed") {
    if (phaseKey === normalizedStage) return "failed";
    if (phaseIndex < currentIndex) return "done";
    return "pending";
  }
  if (buildStatus === "cancelled") {
    if (phaseKey === normalizedStage) return "cancelled";
    if (phaseIndex < currentIndex) return "done";
    return "pending";
  }
  if (buildStatus === "success") return "done";
  if (phaseIndex < currentIndex) return "done";
  if (phaseIndex === currentIndex) return "active";
  return "pending";
}

function buildSkillIndexBuildPhases(
  status: SkillRetrievalStatus | null,
  t: (key: string, options?: Record<string, unknown>) => string
): SkillIndexBuildPhase[] {
  const buildStatus = String(status?.build_status || "idle");
  const currentStage = String(status?.build_stage || (buildStatus === "success" ? "success" : "queued"));
  const installedCount = status?.installed_count ?? status?.installed_enabled_count ?? 0;
  const indexedCount = status?.indexed_count ?? 0;
  const base = [
    {
      key: "queued",
      title: t('skills.retrieval.buildPipeline.queued.title'),
      detail: t('skills.retrieval.buildPipeline.queued.detail'),
    },
    {
      key: "scan",
      title: t('skills.retrieval.buildPipeline.scan.title'),
      detail: t('skills.retrieval.buildPipeline.scan.detail', { count: installedCount }),
    },
    {
      key: "llm_check",
      title: t('skills.retrieval.buildPipeline.llmCheck.title'),
      detail: t('skills.retrieval.buildPipeline.llmCheck.detail'),
    },
    {
      key: "build",
      title: t('skills.retrieval.buildPipeline.build.title'),
      detail: t('skills.retrieval.buildPipeline.build.detail'),
    },
    {
      key: "publish",
      title: t('skills.retrieval.buildPipeline.publish.title'),
      detail: t('skills.retrieval.buildPipeline.publish.detail'),
    },
    {
      key: "success",
      title: t('skills.retrieval.buildPipeline.success.title'),
      detail: t('skills.retrieval.buildPipeline.success.detail', { count: indexedCount || installedCount }),
    },
  ];
  return base.map((phase) => ({
    ...phase,
    state: getSkillIndexBuildPhaseState(phase.key, currentStage, buildStatus),
  }));
}

function getBuildPhaseClass(state: SkillIndexBuildPhaseState): string {
  if (state === "done") return "border-emerald-500/30 bg-emerald-500/10 text-emerald-600";
  if (state === "active") return "border-sky-500/40 bg-sky-500/10 text-sky-600";
  if (state === "failed") return "border-red-500/35 bg-red-500/10 text-red-600";
  if (state === "cancelled") return "border-amber-500/35 bg-amber-500/10 text-amber-600";
  return "border-border bg-secondary/30 text-text-muted";
}

function SkillIndexBuildProgressPanel({
  status,
  progress,
  logs,
  t,
}: {
  status: SkillRetrievalStatus | null;
  progress: number;
  logs: SkillRetrievalBuildLog[];
  t: (key: string, options?: Record<string, unknown>) => string;
}) {
  const phases = buildSkillIndexBuildPhases(status, t);
  const stageLabel = getSkillIndexBuildStageLabel(status?.build_stage, t);
  const isError = status?.build_status === "failed";
  const showPipeline = status?.build_status !== "success";
  return (
    <div className="mt-4 rounded-lg border border-border bg-panel p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-[220px]">
          <div className="text-sm font-medium text-text-strong">
            {t('skills.retrieval.buildMonitorTitle')}
          </div>
          <div className="mt-1 text-xs text-text-muted">
            {t('skills.retrieval.buildMonitorSubtitle', { stage: stageLabel })}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-2 text-xs">
          <div className="rounded-md border border-border bg-secondary/40 px-3 py-2">
            <div className="text-text-muted">{t('skills.retrieval.buildMetric.progress')}</div>
            <div className="mt-1 font-medium text-text-strong">{progress}%</div>
          </div>
          <div className="rounded-md border border-border bg-secondary/40 px-3 py-2">
            <div className="text-text-muted">{t('skills.retrieval.buildMetric.skills')}</div>
            <div className="mt-1 font-medium text-text-strong">
              {status?.installed_count ?? status?.installed_enabled_count ?? 0}
            </div>
          </div>
          <div className="rounded-md border border-border bg-secondary/40 px-3 py-2">
            <div className="text-text-muted">{t('skills.retrieval.buildMetric.indexed')}</div>
            <div className="mt-1 font-medium text-text-strong">{status?.indexed_count ?? 0}</div>
          </div>
        </div>
      </div>

      <div className="mt-4 h-2 overflow-hidden rounded-full bg-secondary">
        <div
          className={`h-full rounded-full  ${isError ? "bg-red-500" : "bg-emerald-500"}`}
          style={{ width: `${progress}%` }}
        />
      </div>

      {showPipeline ? (
        <div className="mt-4 grid gap-4">
          <div className="rounded-md border border-border bg-secondary/30 p-3">
            <div className="mb-3 text-xs font-medium uppercase tracking-wide text-text-muted">
              {t('skills.retrieval.buildPipelineTitle')}
            </div>
            <div className="space-y-2">
              {phases.map((phase, index) => (
                <div key={phase.key} className={`rounded-md border px-3 py-2 ${getBuildPhaseClass(phase.state)}`}>
                  <div className="flex items-center gap-2">
                    <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-current text-[10px]">
                      {index + 1}
                    </span>
                    <span className="min-w-0 truncate text-xs font-medium">{phase.title}</span>
                    <span className="ml-auto text-[10px] uppercase opacity-70">
                      {t(`skills.retrieval.buildPhaseState.${phase.state}`)}
                    </span>
                  </div>
                  <div className="mt-1 pl-7 text-[11px] leading-5 opacity-80">{phase.detail}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}
      {status?.build_message ? (
        <div className="mt-3 rounded-md border border-border bg-secondary/30 px-3 py-2 text-xs text-text-muted">
          {status.build_message}
        </div>
      ) : null}
      {status?.build_error ? (
        <pre className="mt-3 max-h-32 overflow-auto whitespace-pre-wrap rounded border border-red-500/20 bg-red-500/5 p-2 text-xs text-red-600">
          {status.build_error}
        </pre>
      ) : null}
      {logs.length > 0 ? (
        <div className="mt-3 grid gap-1 text-[11px] text-text-muted">
          {logs.slice(-5).map((log, index) => (
            <div key={`${log.time || index}-${log.stage || ""}`} className="flex min-w-0 gap-2">
              <span className="shrink-0 font-mono text-text-muted/70">[{log.stage || "-"}]</span>
              <span className="min-w-0 truncate">{log.message || log.status || ""}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function SkillIndexTreeView({
  roots,
  selectedCid,
  onSelect,
  emptyText,
  branchLabel,
  skillLabel,
  disabledSkillNames,
  disabledSkillLabel,
}: {
  roots: SkillIndexTreeNode[];
  selectedCid: string | null;
  onSelect: (cid: string) => void;
  emptyText: string;
  branchLabel: string;
  skillLabel: string;
  disabledSkillNames: Set<string>;
  disabledSkillLabel: string;
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  useEffect(() => {
    const next: Record<string, boolean> = {};
    const walk = (items: SkillIndexTreeNode[], depth: number) => {
      items.forEach((item) => {
        if (item.children.length > 0 && depth < 2) {
          next[item.cid] = true;
        }
        walk(item.children, depth + 1);
      });
    };
    walk(roots, 0);
    setExpanded(next);
  }, [roots]);

  const renderNode = (node: SkillIndexTreeNode, depth: number): ReactNode => {
    const hasChildren = node.children.length > 0;
    const isExpanded = expanded[node.cid] ?? false;
    const selected = selectedCid === node.cid;
    const isLeaf = node.type === "leaf";
    const disabledLeaf = isLeaf && disabledSkillNames.has(getSkillIndexSkillName(node));
    return (
      <div key={node.cid}>
        <div
          role="treeitem"
          aria-selected={selected}
          aria-expanded={hasChildren ? isExpanded : undefined}
          className={`flex items-center gap-1 rounded-md border text-xs  ${
            getSkillIndexNodeClassName(disabledLeaf, selected)
          }`}
          style={{ paddingLeft: `${8 + depth * 14}px` }}
        >
          <button
            type="button"
            onClick={() => {
              if (hasChildren) {
                setExpanded((prev) => ({ ...prev, [node.cid]: !isExpanded }));
              }
            }}
            className={`h-7 w-5 shrink-0 flex items-center justify-center rounded ${
              hasChildren ? "text-text-muted hover:text-text" : "text-text-muted/50 cursor-default"
            }`}
            aria-label={hasChildren ? (isExpanded ? "Collapse" : "Expand") : undefined}
          >
            {hasChildren ? (
              <ChevronRight
                className={`h-3 w-3  ${isExpanded ? "rotate-90" : ""}`}
                strokeWidth={2}
              />
            ) : (
              <span className="h-1.5 w-1.5 rounded-full bg-current opacity-50" />
            )}
          </button>
          <button
            type="button"
            onClick={() => onSelect(node.cid)}
            className="min-w-0 flex-1 min-h-7 py-1 flex items-center gap-2 text-left"
            title={getSkillIndexNodeLabel(node)}
          >
            <span
              className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] leading-none ${
                getSkillIndexNodeBadgeClassName(disabledLeaf, isLeaf)
              }`}
            >
              {isLeaf ? skillLabel : branchLabel}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate">{getSkillIndexNodeLabel(node)}</span>
              {disabledLeaf ? (
                <span className="block truncate text-[10px] leading-4 text-text-muted">
                  {disabledSkillLabel}
                </span>
              ) : null}
            </span>
          </button>
        </div>
        {hasChildren && isExpanded ? (
          <div className="mt-1 space-y-1">
            {node.children.map((child) => renderNode(child, depth + 1))}
          </div>
        ) : null}
      </div>
    );
  };

  if (roots.length === 0) {
    return <div className="text-sm text-text-muted">{emptyText}</div>;
  }

  return <div className="space-y-1" role="tree">{roots.map((node) => renderNode(node, 0))}</div>;
}

export function SkillPanel({ sessionId, onNavigateToConfig, isActive = false }: SkillPanelProps) {
  const { t, i18n } = useTranslation();
  const readOnly = isEnterprise();
  const [activeTab, setActiveTab] = useState<"my" | "marketplace" | "index" | "graph">("my");
  const [mySkillsSubTab, setMySkillsSubTab] = useState<"all" | "enabled" | "disabled" | "builtin" | "prebuilt" | "user">("all");
  const [mySkillsPage, setMySkillsPage] = useState(1);
  const [mySkillsPageSize, setMySkillsPageSize] = useState(MY_SKILLS_PAGE_SIZE_DEFAULT);
  const [marketplaceSubTab, setMarketplaceSubTab] = useState<"builtin" | "swarmskills" | "online">("builtin");
  // 企业版：技能源为空时不展示 SwarmSkills 入口（null=未知，加载中先展示）
  const [enterpriseSourceCount, setEnterpriseSourceCount] = useState<number | null>(null);
  const [searchTrigger, setSearchTrigger] = useState(0);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [plugins, setPlugins] = useState<InstalledPluginItem[]>([]);
  const [marketplaces, setMarketplaces] = useState<MarketplaceItem[]>([]);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const searchDebounceRef = useRef<number | null>(null);
  const previousListContextRef = useRef<string | null>(null);
  const previousListSessionRef = useRef(sessionId);
  const skillListRequestsRef = useRef(new LatestSkillListRequest());
  /** 首次列表加载成功后置 true：刷新失败（如瞬态 SCOPE_FULL_TIMEOUT）保留旧列表，不清空页面 */
  const skillsLoadedRef = useRef(false);
  const [selectedSkill, setSelectedSkill] = useState<SkillDetail | null>(null);
  const [listState, setListState] = useState<LoadState>("idle");
  const [detailState, setDetailState] = useState<LoadState>("idle");
  const [actionTarget, setActionTarget] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pendingUninstall, setPendingUninstall] = useState<{ name: string; displayName: string; origin?: string } | null>(null);
  const [messageType, setMessageType] = useState<"success" | "error" | "loading" | null>(null);
  const messageTimerRef = useRef<number | null>(null);
  const retrievalPollRef = useRef<number | null>(null);
  const retrievalDiscoveryPollRef = useRef<number | null>(null);
  const retrievalStatusRequestRef = useRef(0);
  const skillGraphPanelRef = useRef<SkillGraphPanelHandle | null>(null);
  const graphReadingStartedAtRef = useRef<number | null>(null);
  const graphReadingTimerRef = useRef<number | null>(null);
  const [graphReading, setGraphReading] = useState(false);
  const [viewMode, setViewMode] = useState<"list" | "grid">("list");
  const [retrievalStatus, setRetrievalStatus] = useState<SkillRetrievalStatus | null>(null);
  const [retrievalTree, setRetrievalTree] = useState("");
  const [retrievalTreeNodes, setRetrievalTreeNodes] = useState<SkillIndexNode[]>([]);
  const [retrievalTreeCounts, setRetrievalTreeCounts] = useState({ branches: 0, skills: 0 });
  const [selectedTreeNodeCid, setSelectedTreeNodeCid] = useState<string | null>(null);
  const [retrievalShowExistingIndexFailureNotice, setRetrievalShowExistingIndexFailureNotice] = useState(false);
  const [retrievalLoading, setRetrievalLoading] = useState<"idle" | "status" | "tree" | "build" | "cancel">("idle");

  // 企业版：加载技能源数量，为空时隐藏 SwarmSkills 入口
  useEffect(() => {
    if (!readOnly) return;
    let cancelled = false;
    setEnterpriseSourceCount(null);
    void fetchEnterpriseSkillSources(sessionId).then((sources) => {
      if (!cancelled) {
        setEnterpriseSourceCount(current => resolveEnterpriseSourceCount(current, sources));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [readOnly, sessionId]);

  useEffect(() => {
    return () => {
      if (messageTimerRef.current !== null) {
        window.clearTimeout(messageTimerRef.current);
      }
      if (searchDebounceRef.current !== null) {
        window.clearTimeout(searchDebounceRef.current);
      }
      if (retrievalPollRef.current !== null) {
        window.clearInterval(retrievalPollRef.current);
      }
      if (retrievalDiscoveryPollRef.current !== null) {
        window.clearInterval(retrievalDiscoveryPollRef.current);
      }
      if (graphReadingTimerRef.current !== null) {
        window.clearTimeout(graphReadingTimerRef.current);
      }
    };
  }, []);

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

  useEffect(() => {
    if (searchDebounceRef.current !== null) {
      window.clearTimeout(searchDebounceRef.current);
    }
    searchDebounceRef.current = window.setTimeout(() => {
      setDebouncedSearch(search);
      searchDebounceRef.current = null;
    }, 500);
  }, [search]);

  const showMessage = useCallback((type: "success" | "error", text: string) => {
    if (messageTimerRef.current !== null) {
      window.clearTimeout(messageTimerRef.current);
    }
    const displayText = type === "success" ? `√ ${text}` : text;
    setMessage(displayText);
    setMessageType(type);
    // 错误信息显示时间更长（8秒），方便用户阅读详细错误描述
    const duration = type === "error" ? 8000 : 3000;
    messageTimerRef.current = window.setTimeout(() => {
      setMessage(null);
      setMessageType(null);
      messageTimerRef.current = null;
    }, duration);
  }, []);
  const [sourceModalOpen, setSourceModalOpen] = useState(false);
  const [skillNetModalOpen, setSkillNetModalOpen] = useState(false);
  const [clawHubModalOpen, setClawHubModalOpen] = useState(false);
  const [teamSkillsHubModalOpen, setTeamSkillsHubModalOpen] = useState(false);
  const [evolutionModalOpen, setEvolutionModalOpen] = useState(false);
  const [evolutionSkillName, setEvolutionSkillName] = useState<string | null>(null);
  const withSession = useCallback(
    (params?: Record<string, unknown>) => ({
      ...(params || {}),
      session_id: sessionId,
    }),
    [sessionId]
  );

  const installedSkillMap = useMemo(() => {
    const map = new Map<string, InstalledPluginItem>();
    plugins.forEach((plugin) => {
      plugin.skills.forEach((skill) => {
        if (!map.has(skill)) {
          map.set(skill, plugin);
        }
      });
    });
    return map;
  }, [plugins]);

  const installedSkillNames = useMemo(
    () => new Set([...installedSkillMap.keys(), ...skills.filter((skill) => skill.installed === true).map((skill) => skill.name)]),
    [installedSkillMap, skills]
  );

  const enterpriseInstalledOrigins = useMemo(
    () => new Set(skills.filter(skill => skill.installed === true && skill.origin).map(skill => skill.origin!)),
    [skills],
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
      result = result.filter((skill) =>
        skill.installed === true ||
        installedSkillMap.has(skill.name) ||
        skill.source === "local" ||
        skill.is_builtin === true ||
        skill.is_builtin_source === true
      );
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
  }, [skills, search, activeTab, installedSkillMap]);

  const visibleSkills = useMemo(() => {
    let filtered = [...filteredSkills];
    if (activeTab === "my") {
      filtered = filtered.filter((skill) => {
        if (skill.is_builtin_source && !skill.is_builtin && skill.installed !== true && !installedSkillMap.has(skill.name) && skill.source !== "local") {
          return false;
        }
        return true;
      });
    }
    return filtered.sort((a, b) => {
      const aSkillNet = a.source === "skillnet" ? 1 : 0;
      const bSkillNet = b.source === "skillnet" ? 1 : 0;
      if (aSkillNet !== bSkillNet) {
        return bSkillNet - aSkillNet;
      }
      return a.name.localeCompare(b.name);
    });
  }, [filteredSkills, activeTab, installedSkillMap]);

  const builtinSkills = useMemo(() => {
    let filtered = skills.filter((skill) => skill.is_builtin === true || skill.is_builtin_source === true);
    if (search.trim()) {
      const searchLower = search.toLowerCase();
      filtered = filtered.filter(
        (skill) =>
          skill.name.toLowerCase().includes(searchLower) ||
          (skill.description && skill.description.toLowerCase().includes(searchLower))
      );
    }
    return filtered;
  }, [skills, search]);

  const fetchMarketplaces = useCallback(async () => {
    try {
      const data = await webRequest<{ marketplaces?: MarketplaceItem[] }>(
        "skills.marketplace.list",
        withSession()
      );
      setMarketplaces(data.marketplaces || []);
    } catch (error) {
      console.error('Failed to load marketplaces:', error);
    }
  }, []);

  const fetchSkills = useCallback(async (refreshMarketplaces = false) => {
    const requestId = skillListRequestsRef.current.begin();
    setListState("loading");
    try {
      const data = await webRequest<{
        skills?: SkillItem[];
        plugins?: InstalledPluginItem[];
      }>(
        "skills.list",
        buildSkillListParams(sessionId, refreshMarketplaces),
        {
          timeoutMs: refreshMarketplaces
            ? SKILLS_FETCH_TIMEOUT_REFRESH_MS
            : SKILLS_FETCH_TIMEOUT_NORMAL_MS,
        }
      );
      if (!skillListRequestsRef.current.isLatest(requestId)) return;
      setSkills((data.skills || []).map(normalizeSkillItem));
      setPlugins(data.plugins || []);
      skillsLoadedRef.current = true;
      setListState("success");

      if (!readOnly) {
        fetchMarketplaces();
      }
    } catch (error) {
      console.error(error);
      if (!skillListRequestsRef.current.isLatest(requestId)) return;
      // 已有成功数据时保留旧列表（stale-while-error），避免页签切换期间瞬态失败清空整个页面
      setListState(skillsLoadedRef.current ? "success" : "error");
    }
  }, [fetchMarketplaces, readOnly, sessionId]);

  const fetchSkillDetail = useCallback(
    async (skillName: string, origin?: string) => {
      setDetailState("loading");
      try {
        const data = await webRequest<SkillDetail>(
          "skills.get",
          withSession({ name: skillName, ...(origin ? { origin } : {}) })
        );
        setSelectedSkill(normalizeSkillItem(data));
        setDetailState("success");
      } catch (error) {
        console.error(error);
        setDetailState("error");
      }
    },
    [withSession]
  );

  const fetchRetrievalStatus = useCallback(async (options?: { silent?: boolean }) => {
    const requestId = ++retrievalStatusRequestRef.current;
    if (!options?.silent) {
      setRetrievalLoading((current) => (current === "idle" ? "status" : current));
    }
    try {
      const data = await webRequest<SkillRetrievalStatus>(
        "skills.retrieval.status",
        withSession()
      );
      if (requestId === retrievalStatusRequestRef.current) {
        setRetrievalStatus(data);
      }
    } catch (error) {
      console.error('Failed to load skill retrieval status:', error);
    } finally {
      if (!options?.silent) {
        setRetrievalLoading((current) => (current === "status" ? "idle" : current));
      }
    }
  }, [withSession]);

  const fetchRetrievalTree = useCallback(async (options?: { silent?: boolean }) => {
    if (!options?.silent) {
      setRetrievalLoading((current) => (current === "idle" ? "tree" : current));
    }
    try {
      const data = await webRequest<SkillRetrievalTreeResponse>(
        "skills.retrieval.tree",
        withSession({ language: i18n.language || "cn" })
      );
      const nodes = Array.isArray(data.nodes) ? data.nodes : [];
      setRetrievalTree(data.result || "");
      setRetrievalTreeNodes(nodes);
      setRetrievalTreeCounts({
        branches: typeof data.branch_count === "number"
          ? data.branch_count
          : nodes.filter((node) => node.type !== "leaf").length,
        skills: typeof data.leaf_count === "number"
          ? data.leaf_count
          : nodes.filter((node) => node.type === "leaf").length,
      });
      setSelectedTreeNodeCid((current) => {
        if (current && nodes.some((node) => node.cid === current)) {
          return current;
        }
        return nodes[0]?.cid || null;
      });
    } catch (error) {
      console.error('Failed to load skill retrieval tree:', error);
      setRetrievalTree(error instanceof Error ? error.message : String(error));
      setRetrievalTreeNodes([]);
      setRetrievalTreeCounts({ branches: 0, skills: 0 });
      setSelectedTreeNodeCid(null);
    } finally {
      if (!options?.silent) {
        setRetrievalLoading((current) => (current === "tree" ? "idle" : current));
      }
    }
  }, [i18n.language, withSession]);

  // 首次挂载、重新进入技能页、切回「我的技能」或切其子页签时刷新一次列表。
  // 用 context 去重，避免首次挂载时两个 effect 同时发出 skills.list。
  useEffect(() => {
    const previousContext = previousListContextRef.current;
    const sessionChanged = previousListSessionRef.current !== sessionId;
    const context = !isActive
      ? "inactive"
      : activeTab === "my"
        ? `my:${mySkillsSubTab}`
        : activeTab;
    if (sessionChanged) {
      skillListRequestsRef.current.begin();
      skillsLoadedRef.current = false;
      setSkills([]);
      setPlugins([]);
    }
    if (shouldFetchSkillList({
      isActive,
      activeTab,
      previousContext,
      currentContext: context,
      sessionChanged,
    })) {
      void fetchSkills();
    }
    previousListContextRef.current = context;
    previousListSessionRef.current = sessionId;
  }, [isActive, activeTab, mySkillsSubTab, fetchSkills, sessionId]);

  useEffect(() => {
    if (readOnly) return;
    fetchRetrievalStatus();
  }, [fetchRetrievalStatus, readOnly]);

  useEffect(() => {
    if (retrievalStatus?.build_status === "running") {
      setRetrievalShowExistingIndexFailureNotice(false);
    }
  }, [retrievalStatus?.build_status]);

  useEffect(() => {
    if (!isActive || activeTab !== "index") return;
    setRetrievalShowExistingIndexFailureNotice(true);
    void fetchRetrievalStatus();
    void fetchRetrievalTree();
  }, [activeTab, fetchRetrievalStatus, fetchRetrievalTree, isActive]);

  useEffect(() => {
    const disabled = retrievalStatus?.enabled === false;
    const running = retrievalStatus?.build_status === "running";
    if (activeTab !== "index" || disabled || !running) {
      if (retrievalPollRef.current !== null) {
        window.clearInterval(retrievalPollRef.current);
        retrievalPollRef.current = null;
      }
      return;
    }
    if (retrievalPollRef.current !== null) return;
    retrievalPollRef.current = window.setInterval(() => {
      void fetchRetrievalStatus({ silent: true });
    }, SKILL_RETRIEVAL_RUNNING_POLL_MS);
    return () => {
      if (retrievalPollRef.current !== null) {
        window.clearInterval(retrievalPollRef.current);
        retrievalPollRef.current = null;
      }
    };
  }, [activeTab, fetchRetrievalStatus, fetchRetrievalTree, retrievalStatus?.build_status, retrievalStatus?.enabled]);

  useEffect(() => {
    const disabled = retrievalStatus?.enabled === false;
    const running = retrievalStatus?.build_status === "running";
    if (activeTab !== "index" || disabled || running) {
      if (retrievalDiscoveryPollRef.current !== null) {
        window.clearInterval(retrievalDiscoveryPollRef.current);
        retrievalDiscoveryPollRef.current = null;
      }
      return;
    }
    if (retrievalDiscoveryPollRef.current !== null) return;
    retrievalDiscoveryPollRef.current = window.setInterval(() => {
      void fetchRetrievalStatus({ silent: true });
    }, SKILL_RETRIEVAL_IDLE_POLL_MS);
    return () => {
      if (retrievalDiscoveryPollRef.current !== null) {
        window.clearInterval(retrievalDiscoveryPollRef.current);
        retrievalDiscoveryPollRef.current = null;
      }
    };
  }, [activeTab, fetchRetrievalStatus, retrievalStatus?.build_status, retrievalStatus?.enabled]);

  useEffect(() => {
    if (activeTab !== "index") return;
    if (retrievalStatus?.build_status === "success" || (retrievalStatus?.index_exists && retrievalStatus?.fresh)) {
      void fetchRetrievalTree();
    }
  }, [
    activeTab,
    fetchRetrievalTree,
    retrievalStatus?.build_status,
    retrievalStatus?.fresh,
    retrievalStatus?.index_exists,
  ]);

  const handleBuildRetrievalIndex = useCallback(async (force = false) => {
    setRetrievalShowExistingIndexFailureNotice(false);
    setRetrievalLoading("build");
    try {
      await webRequest<{ success: boolean; result?: string }>(
        "skills.retrieval.index_build",
        withSession({ force, source: "web" }),
        { timeoutMs: 30_000 }
      );
      await fetchRetrievalStatus();
      await fetchRetrievalTree();
    } catch (error) {
      console.error(error);
    } finally {
      setRetrievalLoading("idle");
    }
  }, [fetchRetrievalStatus, fetchRetrievalTree, withSession]);

  const handleCancelRetrievalBuild = useCallback(async () => {
    setRetrievalLoading("cancel");
    try {
      const result = await webRequest<{ success: boolean; result?: string; build_status?: string }>(
        "skills.retrieval.index_cancel",
        withSession(),
        { timeoutMs: 30_000 }
      );
      if (result.success) {
        setRetrievalStatus((current) => current
          ? {
              ...current,
              build_status: "cancelled",
              build_stage: "cancelled",
              build_message: result.result || current.build_message,
              build_progress: 1,
            }
          : current);
      } else {
        await fetchRetrievalStatus();
      }
    } catch (error) {
      console.error(error);
    } finally {
      setRetrievalLoading("idle");
    }
  }, [fetchRetrievalStatus, withSession]);

  const handleOpenSkill = useCallback(
    (skillName: string, origin?: string) => {
      fetchSkillDetail(skillName, origin);
    },
    [fetchSkillDetail]
  );

  const handleBackToList = useCallback(() => {
    setSelectedSkill(null);
    setDetailState("idle");
  }, []);

  const handleOpenEvolution = useCallback((skillName: string) => {
    setEvolutionSkillName(skillName);
    setEvolutionModalOpen(true);
  }, []);

  const handleCloseEvolution = useCallback(() => {
    setEvolutionModalOpen(false);
    setEvolutionSkillName(null);
  }, []);

  const handleInstall = useCallback(
    async (skillName?: string, forceParam?: boolean) => {
      const targetSkill = skillName
        ? skills.find((skill) => skill.name === skillName)
        : undefined;
      const isForce = forceParam === true;

      // 内置技能的安装：自动使用 builtin marketplace，不需要用户输入
      if (targetSkill?.is_builtin && targetSkill?.is_builtin_source) {
        const spec = `${skillName}@builtin`;
        setActionTarget(spec);
        setMessage(t('skills.messages.installing', { name: skillName }));
        setMessageType("loading");
        let force = isForce;
        let retry = false;
        try {
          do {
            retry = false;
            const data = await webRequest<{
              success: boolean;
              detail?: string;
              detail_key?: string;
              message?: string;
            }>("skills.install", withSession({ spec, force }));
            if (!data.success) {
              // 同名冲突：弹确认框询问是否覆盖
              if (!force && data.detail_key === "skills.builtin.errors.skillAlreadyInstalled") {
                const confirmed = window.confirm(
                  t('skills.builtin.replaceConfirm', { name: skillName })
                );
                if (confirmed) {
                  force = true;
                  retry = true;
                  continue;
                }
                break;
              }
              throw new Error(data.detail || data.message || t('skills.errors.installFailed'));
            }
            showMessage("success", t('skills.messages.installed', { spec: skillName }));
            await fetchSkills();
            if (selectedSkill) {
              await fetchSkillDetail(selectedSkill.name);
            }
          } while (retry);
        } catch (error) {
          console.error(error);
          const errorMessage = error instanceof Error ? error.message : String(error);
          showMessage("error", errorMessage || t('skills.errors.installFailedHint'));
        } finally {
          setActionTarget(null);
        }
        return;
      }

      // 其他技能的安装：提示用户输入 spec
      const marketplaceNames = marketplaces.map((m) => m.name).join(", ");
      const preferredMarketplace =
        targetSkill?.marketplace ||
        (targetSkill &&
        targetSkill.source !== "local" &&
        targetSkill.source !== "project"
          ? targetSkill.source
          : undefined) ||
        marketplaces[0]?.name ||
        "anthropics";
      const defaultSpec = skillName
        ? `${skillName}@${preferredMarketplace}`
        : "plugin-name@anthropics";
      const hint = marketplaceNames
        ? t('skills.marketplacesAvailable', { names: marketplaceNames })
        : t('skills.marketplacesDefault');
      const spec = window.prompt(
        `${t('skills.installPrompt')}\n${hint}`,
        defaultSpec
      );
      if (!spec) return;

      const pluginName = spec.split('@')[0];
      setActionTarget(spec);
      setMessage(t('skills.messages.installing', { name: spec }));
      setMessageType("loading");
      let force = isForce;
      let retry = false;
      try {
        do {
          retry = false;
          const data = await webRequest<{
            success: boolean;
            detail?: string;
            detail_key?: string;
            message?: string;
          }>("skills.install", withSession({ spec, force }));
          if (!data.success) {
            // 同名冲突：弹确认框询问是否覆盖
            if (!force && data.detail_key === "skills.marketplace.errors.skillAlreadyInstalled") {
              const confirmed = window.confirm(
                t('skills.marketplace.replaceConfirm', { name: targetSkill?.display_name || pluginName })
              );
              if (confirmed) {
                force = true;
                retry = true;
                continue;
              }
              break;
            }
            throw new Error(data.detail || data.message || t('skills.errors.installFailed'));
          }
          showMessage("success", t('skills.messages.installed', { spec: skillName || pluginName }));
          await fetchSkills();
          if (selectedSkill) {
            await fetchSkillDetail(selectedSkill.name);
          }
        } while (retry);
      } catch (error) {
        console.error(error);
        showMessage("error", t('skills.errors.installFailedHint'));
      } finally {
        setActionTarget(null);
      }
    },
    [fetchSkills, fetchSkillDetail, selectedSkill, marketplaces, skills, withSession, t]
  );

  const handleImportLocal = useCallback(async () => {
    const path = window.prompt(
      t('skills.importPrompt')
    );
    if (!path) return;

    setActionTarget("import_local");
    setMessage(null);
    setMessageType(null);
    try {
      const data = await webRequest<{
        success: boolean;
        detail?: string;
        message?: string;
        skill?: { name?: string };
      }>("skills.import_local", withSession({
        path,
        force: false,
      }));
      if (!data.success) {
        throw new Error(data.detail || data.message || t('skills.errors.importFailed'));
      }
      showMessage("success", t('skills.messages.imported', { name: data.skill?.name || path }));
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

  const handleUninstall = useCallback((pluginName: string, origin?: string) => {
    if (!pluginName) return;
    const skill = skills.find(item => origin ? item.origin === origin : item.name === pluginName);
    const displayName = skill ? skillPresentationName(skill) : pluginName;
    setPendingUninstall({ name: pluginName, displayName, origin });
  }, [skills]);

  const confirmUninstall = useCallback(async () => {
    if (!pendingUninstall) return;
    const { name, displayName, origin } = pendingUninstall;
    setPendingUninstall(null);
    setActionTarget(name);
    setMessage(null);
    setMessageType(null);
    try {
      // 企业版 skills.uninstall 被网关拦截，卸载用户自装技能须走 skills.enterprise.uninstall
      const data = await webRequest<{
        success: boolean;
        detail?: string;
        message?: string;
        error_message?: string;
      }>(readOnly ? "skills.enterprise.uninstall" : "skills.uninstall", withSession({
        name,
        // 传 origin 让后端按来源精确定位目录与记录，避免重名技能误删另一个
        ...(origin ? { origin } : {}),
      }));
      if (!data.success) {
        throw new Error(data.detail || data.message || data.error_message || t('skills.errors.uninstallFailed'));
      }
      showMessage("success", t('skills.messages.uninstalled', { pluginName: displayName }));
      await fetchSkills();
      handleBackToList();
    } catch (error) {
      console.error(error);
      const errorMessage = error instanceof Error ? error.message : String(error);
      showMessage("error", errorMessage || t('skills.errors.uninstallFailedHint'));
    } finally {
      setActionTarget(null);
    }
  }, [pendingUninstall, fetchSkills, handleBackToList, readOnly, t, withSession]);

  const renderActionButton = (skill: SkillItem) => {
    if (isAdministratorManagedSkill(skill)) {
      return (
        <button
          className="px-4 py-2 rounded-2xl text-sm text-text-muted cursor-not-allowed whitespace-nowrap border border-gray-300"
          disabled
          title={t('skills.enterpriseManagedHint')}
        >
          {t('skills.enterprisePrebuilt')}
        </button>
      );
    }

    // 企业版：能走到这里说明 removable=true（用户自装，如企业 SkillHub 来源），
    // 允许卸载，样式与个人版卸载按钮一致
    if (readOnly) {
      const isLoading = actionTarget === skill.name;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleUninstall(skill.name, skill.origin);
          }}
          className="flex items-center gap-2 px-3 py-1.5 rounded-full text-sm whitespace-nowrap hover:bg-secondary "
          disabled={isLoading}
          style={{ color: 'var(--color-text-primary)' }}
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} style={{ color: 'var(--color-text-primary)' }}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1 1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
          {t('skills.actions.uninstall')}
        </button>
      );
    }

    const plugin = installedSkillMap.get(skill.name);

    // 未安装到用户目录的内置技能（来自内置目录，需要安装）
    // 判断条件：is_builtin_source 为 true 且不在已安装列表中
    const isInstalled = skill.installed === true || installedSkillMap.has(skill.name) || skill.source === "local";
    if (skill.is_builtin_source && !isInstalled) {
      const isLoading = actionTarget === `${skill.name}@builtin`;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleInstall(skill.name);
          }}
          className="skill-action-btn"
          disabled={isLoading}
        >
          {isLoading ? t('skills.actions.installing') : t('skills.actions.install')}
        </button>
      );
    }

    // 用户本地导入的技能（source="local"）允许删除
    if (skill.source === "local") {
      const isLoading = actionTarget === skill.name;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleUninstall(skill.name, skill.origin);
          }}
          className="flex items-center gap-2 px-3 py-1.5 rounded-full text-sm whitespace-nowrap hover:bg-secondary "
          disabled={isLoading}
          style={{ color: 'var(--color-text-primary)' }}
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} style={{ color: 'var(--color-text-primary)' }}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
          {t('skills.actions.uninstall')}
        </button>
      );
    }

    // Marketplace 安装的技能
    if (plugin) {
      const pluginName = plugin.plugin_name || skill.name;
      const isLoading = actionTarget === pluginName;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleUninstall(pluginName);
          }}
          className="flex items-center gap-2 px-3 py-1.5 rounded-full text-sm whitespace-nowrap hover:bg-secondary "
          disabled={isLoading}
          style={{ color: 'var(--color-text-primary)' }}
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} style={{ color: 'var(--color-text-primary)' }}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
          {t('skills.actions.uninstall')}
        </button>
      );
    }

    // Marketplace 中未安装的技能显示安装按钮
    if (skill.source !== "project") {
      const isLoading = Boolean(actionTarget?.startsWith(`${skill.name}@`));
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleInstall(skill.name);
          }}
          className="skill-action-btn"
          disabled={isLoading}
        >
          {isLoading ? t('skills.actions.installing') : t('skills.actions.install')}
        </button>
      );
    }

    // 已安装到用户目录的内置技能（从内置目录复制过来的）
    // 这种情况下 source 可能是 "project"，但 is_builtin_source 为 true
    // 只对已安装的内置技能显示卸载按钮
    if (skill.is_builtin_source && isInstalled) {
      const isLoading = actionTarget === skill.name;
      return (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handleUninstall(skill.name, skill.origin);
          }}
          className="flex items-center gap-2 px-3 py-1.5 rounded-full text-sm whitespace-nowrap hover:bg-secondary "
          disabled={isLoading}
          style={{ color: 'var(--color-text-primary)' }}
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2} style={{ color: 'var(--color-text-primary)' }}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
          </svg>
          {t('skills.actions.uninstall')}
        </button>
      );
    }

    // 默认显示内置（兜底）
    return (
      <button
        className="px-4 py-2 rounded-2xl text-sm text-text-muted cursor-not-allowed whitespace-nowrap border border-gray-300"
        disabled
      >
        {t('skills.builtIn')}
      </button>
    );
  };

  const renderStatus = (skill: SkillItem) => {
    const installed =
      skill.installed === true || installedSkillMap.has(skill.name) || skill.source === "local";
    // 「全部」页签里的条目均已安装，「已安装」徽章冗余，不再展示
    if (installed && mySkillsSubTab === "all") return "";
    if (installed) return t('skills.status.installed');
    if (skill.is_builtin) {
      return "";
    }
    if (skill.source !== "project") return "";
    return t('skills.status.builtIn');
  };

  const isSkillInstalled = (skill: SkillItem): boolean => {
    return skill.installed === true || installedSkillMap.has(skill.name) || skill.source === "local" || skill.source === "project";
  };

  const getMySkillsFiltered = useCallback(() => {
    let filtered = visibleSkills;
    switch (mySkillsSubTab) {
      case "enabled":
        filtered = visibleSkills.filter(s => isSkillInstalled(s) && s.enabled !== false);
        break;
      case "disabled":
        filtered = visibleSkills.filter(s => s.enabled === false);
        break;
      case "builtin":
        filtered = visibleSkills.filter(s => s.source_type === "builtin");
        break;
      case "prebuilt":
        filtered = visibleSkills.filter(s => s.source_type === "prebuilt");
        break;
      case "user":
        filtered = visibleSkills.filter(s => s.source_type === "user");
        break;
      default:
        // 企业版「全部」：企业预置优先，其次内置、用户自装；个人版保持原有顺序
        if (readOnly) {
          const priority = (s: SkillItem) =>
            s.source_type === "prebuilt" ? 0 : s.source_type === "builtin" ? 1 : s.source_type === "user" ? 2 : 3;
          filtered = [...visibleSkills].sort((a, b) => priority(a) - priority(b));
        }
        break;
    }
    return filtered;
  }, [visibleSkills, mySkillsSubTab, installedSkillMap, readOnly]);

  const mySkillsFiltered = useMemo(() => getMySkillsFiltered(), [getMySkillsFiltered]);
  const mySkillsTotalPages = Math.max(1, Math.ceil(mySkillsFiltered.length / mySkillsPageSize));
  const pagedMySkills = useMemo(
    () => mySkillsFiltered.slice(
      (mySkillsPage - 1) * mySkillsPageSize,
      mySkillsPage * mySkillsPageSize
    ),
    [mySkillsFiltered, mySkillsPage, mySkillsPageSize]
  );

  const handleMySkillsPageSizeChange = (nextSize: number) => {
    setMySkillsPageSize(nextSize);
    setMySkillsPage(1);
  };

  useEffect(() => {
    setMySkillsPage(1);
  }, [mySkillsSubTab, debouncedSearch, viewMode]);

  useEffect(() => {
    if (mySkillsPage > mySkillsTotalPages) setMySkillsPage(mySkillsTotalPages);
  }, [mySkillsPage, mySkillsTotalPages]);

  const toggleSkillDisabled = async (skillName: string, origin?: string) => {
    // 找技能时优先按 origin 定位（同名不同源时精确匹配），无 origin 时按 name 回退
    const skill = skills.find(s => s.name === skillName && (!origin || s.origin === origin));
    if (skill && isAdministratorManagedSkill(skill)) return;
    const newEnabled = skill?.enabled === false ? true : false;

    const toggleKey = `toggle:${origin || skillName}`;
    setActionTarget(toggleKey);

    try {
      const result = await webRequest<{
        success: boolean;
        name: string;
        enabled: boolean;
        detail?: string;
      }>(
        "skills.toggle",
        withSession({
          name: skillName,
          enabled: newEnabled,
          // 传 origin 让后端按身份精确匹配 installed_plugin 记录
          ...(origin ? { origin } : {}),
        })
      );

      if (!result.success) {
        throw new Error(result.detail || 'Failed to toggle skill');
      }

      setSkills((prev) =>
        prev.map(s =>
          (s.name === skillName && (!origin || s.origin === origin))
            ? { ...s, enabled: newEnabled }
            : s
        )
      );

      if (selectedSkill && selectedSkill.name === skillName && (!origin || selectedSkill.origin === origin)) {
        setSelectedSkill({ ...selectedSkill, enabled: newEnabled });
      }
    } catch (error) {
      console.error('Failed to toggle skill enabled:', error);
      showMessage('error', t('skills.setEnabledError'));
    } finally {
      setActionTarget(null);
    }
  };

  const renderEvolutionButton = (skill: SkillItem) => {
    const disabled = !skill.has_evolutions;
    if (disabled) {
      return null;
    }
    return (
      <button
        onClick={(event) => {
          event.stopPropagation();
          handleOpenEvolution(skill.name);
        }}
        className="px-4 py-2 rounded-2xl  whitespace-nowrap hover:opacity-80"
        style={{ color: 'var(--color-text-link)', fontSize: '12px' }}
      >
        {t('skills.actions.viewEvolution')}
      </button>
    );
  };

  const cleanMessage = message?.replace("√", "") || "";
  const retrievalTreeRoots = useMemo(
    () => buildSkillIndexTree(retrievalTreeNodes),
    [retrievalTreeNodes]
  );
  const disabledSkillNames = useMemo(
    () => new Set(skills.filter((skill) => skill.enabled === false).map((skill) => skill.name)),
    [skills]
  );
  const selectedTreeNode = useMemo(
    () => findSkillIndexNode(retrievalTreeNodes, selectedTreeNodeCid),
    [retrievalTreeNodes, selectedTreeNodeCid]
  );
  const retrievalUsingExistingAfterFailure = Boolean(
    retrievalStatus
      && retrievalShowExistingIndexFailureNotice
      && retrievalStatus.enabled !== false
      && retrievalStatus.build_status === "failed"
      && retrievalStatus.index_exists
      && retrievalStatus.fresh
  );
  const retrievalUsingExistingAfterCancellation = Boolean(
    retrievalStatus
      && retrievalStatus.enabled !== false
      && retrievalStatus.build_status === "cancelled"
      && retrievalStatus.index_exists
      && retrievalStatus.fresh
  );
  const retrievalUsingExistingAfterInterruptedBuild = (
    retrievalUsingExistingAfterFailure
    || retrievalUsingExistingAfterCancellation
  );
  const retrievalStatusText = retrievalStatus
    ? retrievalStatus.enabled === false
      ? t('skills.retrieval.disabled')
      : retrievalStatus.build_status === "running"
      ? t('skills.retrieval.building')
      : retrievalStatus.build_status === "failed" && !retrievalUsingExistingAfterFailure
      ? t('skills.retrieval.buildFailed')
      : retrievalStatus.build_status === "cancelled"
      ? t('skills.retrieval.cancelled')
      : retrievalStatus.index_exists
      ? retrievalStatus.fresh
        ? t('skills.retrieval.ready')
        : t('skills.retrieval.stale')
      : t('skills.retrieval.missing')
    : t('common.loading');
  const retrievalLastBuildMessage = retrievalUsingExistingAfterFailure
    ? t('skills.retrieval.lastBuildFailedUsingExisting')
    : retrievalUsingExistingAfterCancellation
    ? t('skills.retrieval.lastBuildCancelledUsingExisting')
    : "";
  const retrievalBuildRunning = retrievalStatus?.build_status === "running";
  const retrievalBuildProgress = Math.round(Math.max(0, Math.min(1, retrievalStatus?.build_progress ?? 0)) * 100);
  const retrievalBuildLogs = Array.isArray(retrievalStatus?.build_logs)
    ? retrievalStatus.build_logs.slice(-12)
    : [];
  const retrievalHasBuildInfo = Boolean(
    retrievalStatus
      && retrievalStatus.enabled !== false
      && !retrievalUsingExistingAfterInterruptedBuild
      && (
        retrievalBuildRunning
        || ["success", "failed", "cancelled"].includes(String(retrievalStatus.build_status || ""))
        || retrievalBuildLogs.length > 0
      )
  );
  return (
    <>
      {pendingUninstall && (
        <ConfirmDialog
          title={t('skills.uninstallConfirmTitle')}
          message={t('skills.uninstallConfirm', { pluginName: pendingUninstall.displayName })}
          confirmLabel={t('skills.actions.uninstall')}
          onConfirm={() => void confirmUninstall()}
          onCancel={() => setPendingUninstall(null)}
          loading={actionTarget === pendingUninstall.name}
        />
      )}
      {message && messageType === "success" && (
        <div className="fixed top-4 right-4 z-[9999] rounded-[4px] text-sm text-text shadow-lg flex items-center gap-3 px-4" style={{ backgroundColor: "var(--color-feedback-success-toast)", width: "564px", height: "40px" }}>
          <span className="w-4 h-4 rounded-full bg-[var(--color-feedback-success-indicator)] flex items-center justify-center flex-shrink-0">
            <svg className="w-3 h-3 text-text-inverse" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
            </svg>
          </span>
          {cleanMessage}
          <button
            type="button"
            onClick={() => setMessage(null)}
            className="ml-auto w-6 h-6 flex items-center justify-center hover:bg-card/30 rounded-full "
          >
            <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      )}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        <div className="card flex-1 flex flex-col min-h-0 overflow-hidden">
          <div className="flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold">
              {t('skills.title')}
            </h2>
            <p className="text-sm text-text-muted mt-1">
              {t('skills.subtitle')}
            </p>
          </div>
          <div className="flex items-center">
            {!readOnly && <button
              onClick={() => setSourceModalOpen(true)}
              className="flex items-center gap-1.5 px-1 py-1.5 rounded-lg text-sm text-text-muted hover:text-text hover:bg-secondary/50 "
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
              </svg>
              {t('skills.actions.sourceManager')}
            </button>}
            <button
              onClick={() => {
                if (activeTab === "index") {
                  void fetchRetrievalStatus();
                  void fetchRetrievalTree();
                } else if (activeTab === "graph") {
                  const started = skillGraphPanelRef.current?.refresh() ?? false;
                  if (started) {
                    updateGraphReading(true);
                  }
                } else if (activeTab === "my" || (activeTab === "marketplace" && marketplaceSubTab === "builtin")) {
                  setSearch("");
                  fetchSkills(true);
                } else {
                  setSearchTrigger((prev) => prev + 1);
                }
              }}
              className={`flex items-center gap-1.5 px-1 py-1.5 rounded-lg text-sm text-text-muted  ${
                activeTab === "graph" && graphReading
                  ? "cursor-not-allowed opacity-70"
                  : "hover:text-text hover:bg-secondary/50"
              }`}
              disabled={activeTab === "graph" && graphReading}
            >
              <svg className={`w-4 h-4 ${activeTab === "graph" && graphReading ? "animate-spin" : ""}`} fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
              {activeTab === "graph" && graphReading ? "正在读取技能总谱" : t('common.refresh')}
            </button>
            {!readOnly && <button
              onClick={handleImportLocal}
              className={`flex items-center gap-1.5 px-1 py-1.5 rounded-lg text-sm  ${
                actionTarget === "import_local"
                  ? "text-text-muted cursor-not-allowed"
                  : "text-text-muted hover:text-text hover:bg-secondary/50"
              }`}
              disabled={actionTarget === "import_local"}
            >
              <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 3v9m0 0l-3-3m3 3l3-3" />
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 15v4a2 2 0 002 2h10a2 2 0 002-2v-4" />
              </svg>
              {t('skills.actions.importLocal')}
            </button>}
          </div>
        </div>

        <div className="mt-4 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <button
              onClick={() => setActiveTab("my")}
              className={`px-4 text-sm font-medium  ${
                activeTab === "my"
                  ? "rounded-[8px] bg-secondary h-8 text-text"
                  : "text-text-muted hover:text-text"
              }`}
            >
              {t('skills.tabs.mySkills')}
            </button>
            <button
              onClick={() => {
                setActiveTab("marketplace");
                if (readOnly) setMarketplaceSubTab("swarmskills");
              }}
              className={`px-4 text-sm font-medium  ${
                activeTab === "marketplace"
                  ? "rounded-[8px] bg-secondary h-8 text-text"
                  : "text-text-muted hover:text-text"
              }`}
            >
              {t('skills.tabs.marketplace')}
            </button>
            {!readOnly && (
              <button
                onClick={() => setActiveTab("graph")}
                className={`px-4 text-sm font-medium  ${
                  activeTab === "graph"
                    ? "rounded-[8px] bg-secondary h-8 text-text"
                    : "text-text-muted hover:text-text"
                }`}
              >
                {t('skills.tabs.skillGraph')}
              </button>
            )}
            {!readOnly && (
              <button
                onClick={() => setActiveTab("index")}
                className={`px-4 text-sm font-medium  ${
                  activeTab === "index"
                    ? "rounded-[8px] bg-secondary h-8 text-text"
                    : "text-text-muted hover:text-text"
                }`}
              >
                {t('skills.tabs.skillIndex')}
              </button>
            )}
          </div>
          {activeTab !== "index" && activeTab !== "graph" ? (
            <div className="flex items-center gap-1 border border-border rounded-lg p-1">
              <button
                onClick={() => setViewMode("list")}
                className={`p-1.5 rounded-md  ${
                  viewMode === "list"
                    ? "bg-secondary text-text"
                    : "text-text-muted hover:text-text"
                }`}
                title={t('skills.viewMode.list')}
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
                </svg>
              </button>
              <button
                onClick={() => setViewMode("grid")}
                className={`p-1.5 rounded-md  ${
                  viewMode === "grid"
                    ? "bg-secondary text-text"
                    : "text-text-muted hover:text-text"
                }`}
                title={t('skills.viewMode.grid')}
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h2.25A2.25 2.25 0 0 1 10.5 6v2.25a2.25 2.25 0 0 1-2.25 2.25H6a2.25 2.25 0 0 1-2.25-2.25V6ZM3.75 15.75A2.25 2.25 0 0 1 6 13.5h2.25a2.25 2.25 0 0 1 2.25 2.25V18a2.25 2.25 0 0 1-2.25 2.25H6A2.25 2.25 0 0 1 3.75 18v-2.25ZM13.5 6a2.25 2.25 0 0 1 2.25-2.25H18A2.25 2.25 0 0 1 20.25 6v2.25A2.25 2.25 0 0 1 18 10.5h-2.25a2.25 2.25 0 0 1-2.25-2.25V6ZM13.5 15.75a2.25 2.25 0 0 1 2.25-2.25H18a2.25 2.25 0 0 1 2.25 2.25V18A2.25 2.25 0 0 1 18 20.25h-2.25A2.25 2.25 0 0 1 13.5 18v-2.25Z" />
                </svg>
              </button>
            </div>
          ) : null}
        </div>

        {!readOnly && activeTab === "index" ? (
          <div className="mt-4 flex flex-col flex-1 min-h-0 gap-4 overflow-y-auto pr-2">
            <div className="rounded-lg border border-border bg-panel p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="min-w-[220px]">
                  <div className="text-sm font-medium text-text-strong">
                    {t('skills.retrieval.title')}
                  </div>
                  <div className="text-xs text-text-muted mt-1">
                    {retrievalStatusText}
                    {retrievalStatus?.indexed_count != null
                      ? ` · ${t('skills.retrieval.indexedCount', { count: retrievalStatus.indexed_count })}`
                      : ""}
                    {(retrievalStatus?.installed_count ?? retrievalStatus?.installed_enabled_count) != null
                      ? ` · ${t('skills.retrieval.installedCount', {
                          count: retrievalStatus?.installed_count ?? retrievalStatus?.installed_enabled_count,
                        })}`
                      : ""}
                  </div>
                  {retrievalLastBuildMessage ? (
                    <div className="mt-1 text-xs text-amber-600">
                      {retrievalLastBuildMessage}
                    </div>
                  ) : null}
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => void handleBuildRetrievalIndex(false)}
                    className="px-3 py-1.5 rounded-lg text-sm border border-border hover:bg-secondary  disabled:opacity-60"
                    disabled={retrievalLoading === "build" || retrievalBuildRunning || retrievalStatus?.enabled === false}
                  >
                    {retrievalLoading === "build"
                      ? t('skills.retrieval.building')
                      : t('skills.retrieval.build')}
                  </button>
                  {retrievalStatus?.index_exists ? (
                    <button
                      onClick={() => void handleBuildRetrievalIndex(true)}
                      className="px-3 py-1.5 rounded-lg text-sm border border-border hover:bg-secondary  disabled:opacity-60"
                      disabled={retrievalLoading === "build" || retrievalBuildRunning || retrievalStatus?.enabled === false}
                    >
                      {retrievalLoading === "build"
                        ? t('skills.retrieval.building')
                        : t('skills.retrieval.fullRebuild')}
                    </button>
                  ) : null}
                  {retrievalBuildRunning ? (
                    <button
                      onClick={handleCancelRetrievalBuild}
                      className="px-3 py-1.5 rounded-lg text-sm border border-border hover:bg-secondary  disabled:opacity-60"
                      disabled={retrievalLoading === "cancel"}
                    >
                      {retrievalLoading === "cancel"
                        ? t('skills.retrieval.cancelling')
                        : t('skills.retrieval.cancel')}
                    </button>
                  ) : null}
                  <button
                    onClick={() => {
                      setRetrievalShowExistingIndexFailureNotice(true);
                      void fetchRetrievalStatus();
                      void fetchRetrievalTree();
                    }}
                    className="px-3 py-1.5 rounded-lg text-sm border border-border hover:bg-secondary  disabled:opacity-60"
                    disabled={retrievalLoading === "tree" || retrievalLoading === "status"}
                  >
                    {retrievalLoading === "tree" || retrievalLoading === "status"
                      ? t('common.refreshing')
                      : t('common.refresh')}
                  </button>
                </div>
              </div>
              {retrievalHasBuildInfo ? (
                <SkillIndexBuildProgressPanel
                  status={retrievalStatus}
                  progress={retrievalBuildProgress}
                  logs={retrievalBuildLogs}
                  t={t}
                />
              ) : null}
            </div>
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(320px,1fr)_minmax(320px,0.9fr)]">
              <div className="rounded-lg border border-border bg-panel p-4 min-h-[420px] flex flex-col">
                <div className="mb-3 flex items-center justify-between gap-2">
                  <div>
                    <div className="text-sm font-medium text-text-strong">
                      {t('skills.retrieval.treeTitle')}
                    </div>
                    <div className="text-xs text-text-muted mt-1">
                      {retrievalTreeNodes.length > 0
                        ? t('skills.retrieval.treeCount', {
                            branches: retrievalTreeCounts.branches,
                            skills: retrievalTreeCounts.skills,
                          })
                        : retrievalLoading === "tree"
                        ? t('common.loading')
                        : t('skills.retrieval.noTree')}
                    </div>
                  </div>
                </div>
                <div className="flex-1 min-h-[320px] overflow-auto rounded-md border border-border bg-secondary/40 p-2">
                  {retrievalTreeNodes.length > 0 ? (
                    <SkillIndexTreeView
                      roots={retrievalTreeRoots}
                      selectedCid={selectedTreeNodeCid}
                      onSelect={setSelectedTreeNodeCid}
                      emptyText={t('skills.retrieval.noTree')}
                      branchLabel={t('skills.retrieval.nodeTypes.branch')}
                      skillLabel={t('skills.retrieval.nodeTypes.skill')}
                      disabledSkillNames={disabledSkillNames}
                      disabledSkillLabel={t('skills.retrieval.disabledSkill')}
                    />
                  ) : (
                    <MarkdownRenderer
                      content={
                        retrievalTree
                        || (retrievalLoading === "tree" ? t('common.loading') : t('skills.retrieval.noTree'))
                      }
                      className="chat-markdown text-xs text-text-muted"
                    />
                  )}
                </div>
              </div>
              <div className="rounded-lg border border-border bg-panel p-4 min-h-[420px] flex flex-col">
                <div className="text-sm font-medium text-text-strong mb-3">
                  {t('skills.retrieval.nodeDetails')}
                </div>
                {selectedTreeNode ? (
                  <div className="flex-1 min-h-0 overflow-auto">
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="text-base font-semibold text-text-strong break-words">
                          {getSkillIndexNodeLabel(selectedTreeNode)}
                        </div>
                        <div className="mt-1 text-xs text-text-muted break-all">
                          {selectedTreeNode.cid}
                        </div>
                      </div>
                      <span
                        className={`shrink-0 rounded border px-2 py-1 text-xs ${
                          selectedTreeNode.type === "leaf"
                            ? "border-emerald-500/25 bg-emerald-500/10 text-emerald-600"
                            : "border-sky-500/25 bg-sky-500/10 text-sky-600"
                        }`}
                      >
                        {selectedTreeNode.type === "leaf"
                          ? t('skills.retrieval.nodeTypes.skill')
                          : t('skills.retrieval.nodeTypes.branch')}
                      </span>
                    </div>

                    <dl className="mt-4 space-y-3 text-sm">
                      <div>
                        <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeDescription')}</dt>
                        <dd className="mt-1 whitespace-pre-wrap text-text">
                          {selectedTreeNode.description || t('skills.noDescription')}
                        </dd>
                      </div>
                      {selectedTreeNode.select_when ? (
                        <div>
                          <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeSelectWhen')}</dt>
                          <dd className="mt-1 whitespace-pre-wrap text-text">{selectedTreeNode.select_when}</dd>
                        </div>
                      ) : null}
                      {selectedTreeNode.dont_select_when ? (
                        <div>
                          <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeDontSelectWhen')}</dt>
                          <dd className="mt-1 whitespace-pre-wrap text-text">{selectedTreeNode.dont_select_when}</dd>
                        </div>
                      ) : null}
                      {selectedTreeNode.source_description ? (
                        <div>
                          <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeSourceDescription')}</dt>
                          <dd className="mt-1 whitespace-pre-wrap text-text">{selectedTreeNode.source_description}</dd>
                        </div>
                      ) : null}
                      {selectedTreeNode.worker_id ? (
                        <div>
                          <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeWorkerId')}</dt>
                          <dd className="mt-1 break-all font-mono text-xs text-text">{selectedTreeNode.worker_id}</dd>
                        </div>
                      ) : null}
                      {selectedTreeNode.category ? (
                        <div>
                          <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeCategory')}</dt>
                          <dd className="mt-1 whitespace-pre-wrap text-text">{selectedTreeNode.category}</dd>
                        </div>
                      ) : null}
                      {selectedTreeNode.keywords?.length ? (
                        <div>
                          <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeKeywords')}</dt>
                          <dd className="mt-2 flex flex-wrap gap-1.5">
                            {selectedTreeNode.keywords.slice(0, 24).map((keyword) => (
                              <span key={keyword} className="rounded border border-border bg-secondary px-2 py-0.5 text-xs text-text-muted">
                                {keyword}
                              </span>
                            ))}
                          </dd>
                        </div>
                      ) : null}
                      {selectedTreeNode.examples?.length ? (
                        <div>
                          <dt className="text-xs text-text-muted">{t('skills.retrieval.nodeExamples')}</dt>
                          <dd className="mt-1 space-y-1">
                            {selectedTreeNode.examples.slice(0, 5).map((example) => (
                              <div key={example} className="whitespace-pre-wrap rounded border border-border bg-secondary px-2 py-1 text-xs text-text">
                                {example}
                              </div>
                            ))}
                          </dd>
                        </div>
                      ) : null}
                    </dl>
                  </div>
                ) : (
                  <div className="flex-1 min-h-[220px] rounded-md border border-dashed border-border bg-secondary/30 p-4 text-sm text-text-muted">
                    {t('skills.retrieval.selectNodeHint')}
                  </div>
                )}
              </div>
            </div>
          </div>
          ) : null}

        {!readOnly && activeTab === "graph" ? (
          <div className="mt-4 flex-1 min-h-0">
            <SkillGraphPanel ref={skillGraphPanelRef} onReadingChange={updateGraphReading} />
          </div>
        ) : null}

        {activeTab === "marketplace" ? (
          <>
            <div className="mt-4 flex items-center justify-between gap-4">
              <div className="flex items-center gap-2">
                {!readOnly && (
                  <button
                    onClick={() => {
                      setMarketplaceSubTab("builtin");
                      setDebouncedSearch(search);
                      setSearchTrigger((prev) => prev + 1);
                    }}
                    className={`px-4 text-sm font-medium  ${
                      marketplaceSubTab === "builtin"
                        ? "rounded-[8px] bg-secondary h-8 text-text"
                        : "text-text-muted hover:text-text"
                    }`}
                  >
                    {t('skills.marketplaceTabs.builtin')}
                  </button>
                )}
              {enterpriseSourceCount !== 0 && (
                <button
                  onClick={() => {
                    setMarketplaceSubTab("swarmskills");
                    setDebouncedSearch(search);
                    setSearchTrigger((prev) => prev + 1);
                  }}
                  className={`px-4 text-sm font-medium  ${
                    marketplaceSubTab === "swarmskills"
                      ? "rounded-[8px] bg-secondary h-8 text-text"
                      : "text-text-muted hover:text-text"
                  }`}
                >
                  {t('skills.swarmskills.title')}
                </button>
              )}
              {!readOnly && (
                <button
                  onClick={() => {
                    setMarketplaceSubTab("online");
                    setDebouncedSearch(search);
                    setSearchTrigger((prev) => prev + 1);
                  }}
                  className={`px-4 text-sm font-medium  ${
                    marketplaceSubTab === "online"
                      ? "rounded-[8px] bg-secondary h-8 text-text"
                      : "text-text-muted hover:text-text"
                  }`}
                >
                  {t('skills.onlineSearch.title')}
                </button>
              )}
              </div>
              <div className="flex-1">
                <input
                  type="text"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={
                    marketplaceSubTab === "builtin"
                      ? t("skills.searchPlaceholder")
                      : marketplaceSubTab === "swarmskills"
                      ? t("skills.swarmskills.searchPlaceholder")
                      : t("skills.onlineSearch.searchPlaceholder")
                  }
                  className="w-full px-3 py-1.5 rounded-lg text-sm bg-secondary border border-border text-text placeholder:text-text-muted"
                />
              </div>
            </div>

            <div className={`mt-4 flex-1 min-h-0 overflow-y-auto ${viewMode === "grid" && marketplaceSubTab === "builtin" ? "flex flex-wrap gap-4 content-start" : "space-y-3"}`}>
              {marketplaceSubTab === "builtin" && (
                <>
                  {listState === "loading" && (
                    <div className="flex items-center justify-center h-full text-text-muted">{t('common.loading')}</div>
                  )}
                  {listState === "error" && (
                    <div className="text-sm text-text-muted">{t('skills.listError')}</div>
                  )}
                  {listState === "success" && builtinSkills.length === 0 && (
                    <div className="text-sm text-text-muted">{t('skills.noMatches')}</div>
                  )}
                  {listState === "success" && builtinSkills.length > 0 && (
                    builtinSkills.map((skill) => {
                      const avatar = getSkillAvatar(skill.name);
                      const displayName = skill.market_display_name || skill.display_name || skill.name;
                      const isDisabled = skill.enabled === false;
                      const isToggling = actionTarget === `toggle:${skill.name}`;
                      const isInstalled = skill.installed === true || installedSkillMap.has(skill.name) || skill.source === "local";
                      const isInstalling = actionTarget === `${skill.name}@builtin`;
                      return (
                        <div
                          key={skill.origin || skill.name}
                          onClick={() => handleOpenSkill(skill.name, skill.origin)}
                          className={`text-left border border-border bg-panel hover:bg-card  cursor-pointer ${viewMode === "grid" ? "rounded-[8px] p-4 flex flex-col" : "w-full rounded-lg p-4"}`}
                          style={viewMode === "grid" ? { width: "496px", height: "168px", flexShrink: 0 } : undefined}
                        >
                          {viewMode === "list" ? (
                            <div className="flex items-center justify-between gap-4">
                              <div className="flex items-center gap-3 min-w-0 flex-1">
                                <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}>
                                  {avatar.firstChar}
                                </div>
                                <div className="min-w-0">
                                  <div className="text-base font-semibold text-text-strong">
                                    {displayName}
                                  </div>
                                  <div className="text-sm text-text-muted mt-1 line-clamp-3">
                                    {skillDisplayDesc(skill, t('skills.noDescription'))}
                                  </div>
                                </div>
                              </div>
                              <div className="flex items-center gap-4 flex-shrink-0">
                                {skill.is_builtin_source && !isInstalled ? (
                                  <button
                                    onClick={(event) => {
                                      event.stopPropagation();
                                      handleInstall(skill.name);
                                    }}
                                    className="min-w-[76px] h-[28px] px-3 text-sm rounded-full border border-black bg-card text-text hover:bg-gray-100  whitespace-nowrap"
                                    disabled={isInstalling}
                                  >
                                    {isInstalling ? t('skills.actions.installing') : t('skills.actions.install')}
                                  </button>
                                ) : (
                                  <Switch
                                    checked={!isDisabled}
                                    onChange={() => toggleSkillDisabled(skill.name, skill.origin)}
                                    disabled={readOnly || isToggling}
                                  />
                                )}
                              </div>
                            </div>
                          ) : (
                            <>
                              <div className="flex items-start gap-3 flex-shrink-0">
                                <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
                                  {avatar.firstChar}
                                </div>
                                <div className="min-w-0 flex-1">
                                  <div className="text-sm font-semibold text-text-strong truncate">
                                    {displayName}
                                  </div>
                                  <div className="text-xs text-text-muted mt-1 line-clamp-2">
                                    {skillDisplayDesc(skill, t('skills.noDescription'))}
                                  </div>
                                </div>
                              </div>
                              <div className="flex flex-wrap gap-1.5 mt-2 flex-shrink-0 text-xs text-text-muted">
                                <span className="px-2 py-0.5 rounded-full bg-secondary border border-border truncate">
                                  {t('skills.sourceLabel')}: {getSourceLabel(skill.source, t, skill.is_builtin_source, skill.source_type)}
                                </span>
                              </div>
                              <div className="flex items-center mt-auto pt-2 gap-2 flex-shrink-0" style={{ width: "100%" }}>
                                <div className="flex gap-1.5 flex-1">
                                  {!readOnly && renderEvolutionButton(skill)}
                                </div>
                                <div className="flex-shrink-0 ml-auto">
                                  {!readOnly && renderActionButton(skill)}
                                </div>
                              </div>
                            </>
                          )}
                        </div>
                      );
                    })
                  )}
                </>
              )}

              {marketplaceSubTab === "swarmskills" && (
                <div className="h-full" key={`swarmskills-${searchTrigger}`}>
                  {readOnly ? (
                    <EnterpriseSkillSourcePanel
                      sessionId={sessionId}
                      installedSkillOrigins={enterpriseInstalledOrigins}
                      installedStateLoaded={skillsLoadedRef.current}
                      viewMode={viewMode}
                      externalSearchQuery={debouncedSearch}
                      onInstalled={() => fetchSkills()}
                    />
                  ) : (
                    <TeamSkillsHubModal
                      open={true}
                      embedded={true}
                      sessionId={sessionId}
                      externalSearchQuery={debouncedSearch}
                      installedSkillOrigins={installedSkillOrigins}
                      viewMode={viewMode}
                      onClose={() => {}}
                      onInstalled={(_skillName: string) => {
                        void fetchSkills();
                      }}
                    />
                  )}
                </div>
              )}

              {marketplaceSubTab === "online" && (
                <div className="h-full" key={`online-${searchTrigger}`}>
                  <OnlineSkillSearchPanel
                    sessionId={sessionId}
                    externalSearchQuery={debouncedSearch}
                    installedSkillNames={installedSkillNames}
                    installedSkillOrigins={installedSkillOrigins}
                    viewMode={viewMode}
                    onInstalled={(_skillName: string) => {
                      void fetchSkills();
                    }}
                  />
                </div>
              )}
            </div>
          </>
        ) : null}

        {activeTab === "my" ? (
          <>
            {message && messageType === "error" && (
              <div className="mt-3 px-3 py-2 rounded-md bg-secondary text-sm text-danger">
                {message}
              </div>
            )}
            {selectedSkill ? (
              <div className="mt-4 flex-1 overflow-y-auto">
                <div className="text-sm text-text-muted mb-3">
                  {detailState === "loading" && t('skills.detailLoading')}
                  {detailState === "error" && t('skills.detailError')}
                </div>

                <div className="rounded-lg border border-border bg-panel p-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex items-start gap-3">
                      <button
                        onClick={handleBackToList}
                        className="flex-shrink-0 w-10 h-10 rounded-lg flex items-center justify-center text-text-muted hover:text-text hover:bg-secondary/50 "
                      >
                        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
                        </svg>
                      </button>
                      <div className={`w-10 h-10 rounded-lg ${getSkillAvatar(selectedSkill.name).color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}>
                        {getSkillAvatar(selectedSkill.name).firstChar}
                      </div>
                      <div>
                        <div className="text-lg font-semibold text-text-strong">
                          {selectedSkill.market_display_name || selectedSkill.display_name || selectedSkill.name}
                        </div>
                        <div className="text-sm text-text-muted mt-1">
                          {skillDisplayDesc(selectedSkill, t('skills.noDescription'))}
                        </div>
                        <div className="flex flex-wrap gap-2 mt-3 text-xs text-text-muted">
                          <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                            {t('skills.sourceLabel')}: {getSourceLabel(selectedSkill.source, t, selectedSkill.is_builtin_source, selectedSkill.source_type)}
                          </span>
                          <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                            {t('skills.versionLabel')}: {selectedSkill.version || 'unknown'}
                          </span>
                          <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                            {t('skills.authorLabel')}: {selectedSkill.author || 'unknown'}
                          </span>
                        </div>
                      </div>
                    </div>

                    <div className="flex flex-col items-end gap-2">
                      <div className="flex items-center gap-4">
                        {!readOnly && (
                          <div className="flex items-center gap-2">
                            <span className="text-sm whitespace-nowrap" style={{ color: 'var(--color-text-primary)' }}>{selectedSkill.enabled === false ? t('skills.mySkillsTabs.disabled') : t('skills.mySkillsTabs.enabled')}</span>
                            <Switch
                              checked={selectedSkill.enabled !== false}
                              onChange={() => toggleSkillDisabled(selectedSkill.name, selectedSkill.origin)}
                              disabled={
                                actionTarget === `toggle:${selectedSkill.origin || selectedSkill.name}` ||
                                isAdministratorManagedSkill(selectedSkill)
                              }
                            />
                          </div>
                        )}
                        {renderActionButton(selectedSkill)}
                      </div>
                      {!readOnly && renderEvolutionButton(selectedSkill)}
                    </div>
                  </div>

                  <div className="mt-4">
                    <div className="text-sm font-medium text-text mb-2">
                      {t('skills.allowedTools')}
                    </div>
                    <div className="flex flex-wrap gap-2 text-xs text-text-muted">
                      {selectedSkill.allowed_tools?.length ? (
                        selectedSkill.allowed_tools.map((tool) => (
                          <span
                            key={tool}
                            className="px-2 py-1 rounded-full bg-secondary border border-border"
                          >
                            {tool}
                          </span>
                        ))
                      ) : (
                        <span className="text-text-muted">{t('skills.unlimited')}</span>
                      )}
                    </div>
                  </div>

                  <div className="mt-4">
                    <div className="text-sm font-medium text-text mb-2">
                      {t('skills.contentPreview')}
                    </div>
                    <div className="text-sm text-text bg-secondary border border-border rounded-md p-3">
                      {selectedSkill.content ? (
                        <MarkdownRenderer content={selectedSkill.content} className="chat-markdown text-sm" />
                      ) : (
                        t('skills.noContent')
                      )}
                    </div>
                  </div>
                </div>
              </div>
            ) : (
              <div className="mt-4 flex flex-col flex-1 min-h-0">
                <div className="flex items-center gap-3 flex-shrink-0">
                  <div className="flex items-center gap-2">
                    {readOnly ? (
                      <>
                        <button
                          onClick={() => setMySkillsSubTab("all")}
                          className={`px-4 text-sm font-medium  ${
                            mySkillsSubTab === "all"
                              ? "rounded-[8px] bg-secondary h-8 text-text"
                              : "text-text-muted hover:text-text"
                          }`}
                        >
                          {t('skills.mySkillsTabs.all')}
                        </button>
                        <button
                          onClick={() => setMySkillsSubTab("builtin")}
                          className={`px-4 text-sm font-medium  ${
                            mySkillsSubTab === "builtin"
                              ? "rounded-[8px] bg-secondary h-8 text-text"
                              : "text-text-muted hover:text-text"
                          }`}
                        >
                          {t('skills.mySkillsTabs.builtin')}
                        </button>
                        <button
                          onClick={() => setMySkillsSubTab("prebuilt")}
                          className={`px-4 text-sm font-medium  ${
                            mySkillsSubTab === "prebuilt"
                              ? "rounded-[8px] bg-secondary h-8 text-text"
                              : "text-text-muted hover:text-text"
                          }`}
                        >
                          {t('skills.mySkillsTabs.prebuilt')}
                        </button>
                        <button
                          onClick={() => setMySkillsSubTab("user")}
                          className={`px-4 text-sm font-medium  ${
                            mySkillsSubTab === "user"
                              ? "rounded-[8px] bg-secondary h-8 text-text"
                              : "text-text-muted hover:text-text"
                          }`}
                        >
                          {t('skills.mySkillsTabs.user')}
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          onClick={() => setMySkillsSubTab("all")}
                          className={`px-4 text-sm font-medium  ${
                            mySkillsSubTab === "all"
                              ? "rounded-[8px] bg-secondary h-8 text-text"
                              : "text-text-muted hover:text-text"
                          }`}
                        >
                          {t('skills.mySkillsTabs.all')}
                        </button>
                        <button
                          onClick={() => setMySkillsSubTab("enabled")}
                          className={`px-4 text-sm font-medium  ${
                            mySkillsSubTab === "enabled"
                              ? "rounded-[8px] bg-secondary h-8 text-text"
                              : "text-text-muted hover:text-text"
                          }`}
                        >
                          {t('skills.mySkillsTabs.enabled')}
                        </button>
                        <button
                          onClick={() => setMySkillsSubTab("disabled")}
                          className={`px-4 text-sm font-medium  ${
                            mySkillsSubTab === "disabled"
                              ? "rounded-[8px] bg-secondary h-8 text-text"
                              : "text-text-muted hover:text-text"
                          }`}
                        >
                          {t('skills.mySkillsTabs.disabled')}
                        </button>
                      </>
                    )}
                  </div>
                  <div className="flex-1 min-w-0">
                    <input
                      value={search}
                      onChange={(event) => setSearch(event.target.value)}
                      placeholder={t('skills.searchPlaceholder')}
                      className="w-full px-3 py-2 rounded-md bg-panel border border-border text-sm text-text placeholder:text-text-muted"
                    />
                  </div>
                </div>

                <div className={`mt-4 flex-1 min-h-0 overflow-y-auto ${viewMode === "grid" ? "flex flex-wrap gap-4 content-start" : "space-y-3"}`}>
                  {listState === "loading" && (
                    <div className="flex items-center justify-center h-full text-text-muted">{t('common.loading')}</div>
                  )}
                  {listState === "error" && (
                    <div className="text-sm text-text-muted">
                      {t('skills.listError')}
                    </div>
                  )}
                  {listState === "success" && mySkillsFiltered.length === 0 && (
                    <div className="text-sm text-text-muted">
                      {mySkillsSubTab === "disabled" ? t('skills.noDisabledSkills') : 
                       mySkillsSubTab === "enabled" ? t('skills.noEnabledSkills') :
                       t('skills.noMatches')}
                    </div>
                  )}
                  {listState === "success" &&
                    pagedMySkills.map((skill) => {
                      const avatar = getSkillAvatar(skill.name);
                      const displayName = skill.market_display_name || skill.display_name || skill.name;
                      const isDisabled = skill.enabled === false;
                      const isToggling = actionTarget === `toggle:${skill.origin || skill.name}`;
                      return (
                        <div
                          key={skill.origin || skill.name}
                          onClick={() => handleOpenSkill(skill.name, skill.origin)}
                          className={`text-left border border-border bg-panel hover:bg-card  cursor-pointer ${viewMode === "grid" ? "rounded-[8px] p-4 flex flex-col" : "w-full rounded-lg p-4"}`}
                          style={viewMode === "grid" ? { width: "496px", height: "168px", flexShrink: 0 } : undefined}
                        >
                          {viewMode === "list" ? (
                            <div className="flex items-center justify-between gap-4">
                              <div className="flex items-center gap-3 min-w-0 flex-1">
                                <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold`}>
                                  {avatar.firstChar}
                                </div>
                                <div className="min-w-0">
                                  <div className="text-base font-semibold text-text-strong">
                                    {displayName}
                                  </div>
                                  <div className="text-sm text-text-muted mt-1 line-clamp-3">
                                    {skillDisplayDesc(skill, t('skills.noDescription'))}
                                  </div>
                                  {(!readOnly || mySkillsSubTab === "all") && (
                                    <div className="flex flex-wrap gap-2 mt-3 text-xs text-text-muted">
                                      <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                                        {t('skills.sourceLabel')}: {getSourceLabel(skill.source, t, skill.is_builtin_source, skill.source_type)}
                                      </span>
                                      {renderStatus(skill) && (
                                        <span className="px-2 py-1 rounded-full bg-secondary border border-border">
                                          {t('skills.statusLabel')}: {renderStatus(skill)}
                                        </span>
                                      )}
                                    </div>
                                  )}
                                </div>
                              </div>
                              {!readOnly && (
                                <div className="flex flex-col items-end gap-2 flex-shrink-0">
                                  {renderEvolutionButton(skill)}
                                  <div className="flex items-center gap-2">
                                    <Switch
                                      checked={!isDisabled}
                                      onChange={() => toggleSkillDisabled(skill.name, skill.origin)}
                                      disabled={isToggling || isAdministratorManagedSkill(skill)}
                                    />
                                  </div>
                                </div>
                              )}
                            </div>
                          ) : (
                            <>
                              <div className="flex items-start gap-3 flex-shrink-0">
                                <div className={`w-10 h-10 rounded-lg ${avatar.color} flex items-center justify-center flex-shrink-0 text-text-inverse font-semibold text-sm`}>
                                  {avatar.firstChar}
                                </div>
                                <div className="min-w-0 flex-1">
                                  <div className="text-sm font-semibold text-text-strong truncate">
                                    {displayName}
                                  </div>
                                  <div className="text-xs text-text-muted mt-1 line-clamp-2">
                                    {skillDisplayDesc(skill, t('skills.noDescription'))}
                                  </div>
                                </div>
                              </div>
                              <div className="flex flex-wrap gap-1.5 mt-2 flex-shrink-0 text-xs text-text-muted">
                                {(!readOnly || mySkillsSubTab === "all") && (
                                  <>
                                    <span className="px-2 py-0.5 rounded-full bg-secondary border border-border truncate">
                                      {t('skills.sourceLabel')}: {getSourceLabel(skill.source, t, skill.is_builtin_source, skill.source_type)}
                                    </span>
                                    {renderStatus(skill) && (
                                      <span className="px-2 py-0.5 rounded-full bg-secondary border border-border truncate">
                                        {t('skills.statusLabel')}: {renderStatus(skill)}
                                      </span>
                                    )}
                                  </>
                                )}
                              </div>
                              {!readOnly && (
                                <div className="flex items-center mt-auto pt-2 gap-2 flex-shrink-0" style={{ width: "100%" }}>
                                  <div className="flex gap-1.5 flex-1">
                                    {renderEvolutionButton(skill)}
                                  </div>
                                  <div className="flex items-center gap-2">
                                    <Switch
                                      checked={!isDisabled}
                                      onChange={() => toggleSkillDisabled(skill.name, skill.origin)}
                                      disabled={isToggling || isAdministratorManagedSkill(skill)}
                                    />
                                  </div>
                                </div>
                              )}
                            </>
                          )}
                        </div>
                      );
                    })}
                </div>
                {listState === "success" && (
                  <Pagination
                    page={mySkillsPage}
                    totalPages={mySkillsTotalPages}
                    total={mySkillsFiltered.length}
                    pageSize={mySkillsPageSize}
                    onPageSizeChange={handleMySkillsPageSizeChange}
                    onPageChange={(page) => setMySkillsPage(page)}
                    className="mt-3"
                  />
                )}
              </div>
            )}
          </>
        ) : null}
      </div>
      <SourceManagerModal
        open={sourceModalOpen}
        sessionId={sessionId}
        onClose={() => setSourceModalOpen(false)}
        onNavigateToConfig={() => {
          setSourceModalOpen(false);
          onNavigateToConfig?.();
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
        onNavigateToConfig={() => {
          setSkillNetModalOpen(false);
          onNavigateToConfig?.();
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
        installedSkillOrigins={installedSkillOrigins}
        onClose={() => setTeamSkillsHubModalOpen(false)}
        onInstalled={async () => {
          await fetchSkills();
        }}
      />
      <SkillEvolutionModal
        open={evolutionModalOpen}
        sessionId={sessionId}
        skillName={evolutionSkillName}
        onClose={handleCloseEvolution}
        onSaved={async () => {
          await fetchSkills();
          if (selectedSkill) {
            await fetchSkillDetail(selectedSkill.name);
          }
        }}
      />
    </div>
      </>
    );
}
