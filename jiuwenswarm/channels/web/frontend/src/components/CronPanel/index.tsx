import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronLeft, ChevronRight, TrendingUp, Newspaper, Briefcase } from 'lucide-react';
import { webRequest, webClient } from '../../services/webClient';
import { useCronStore } from '../../stores';
import { projectRegistryClient } from '../../features/workspace/projectRegistryClient';
import type { ProjectInfo } from '../../features/workspace/projectTypes';
import type { Session } from '../../types';
import type { CronJobDTO, CronTaskUI, CronTemplateUI } from '../../types/cron';
import { CRON_TEMPLATES } from './constants';
import { isTeamCronModeValue } from './cronMode';
import { normalizeWakeOffsetSeconds } from './cronWakeOffset';
import { cronExprToSchedule, summarizeSchedule } from './scheduleConvert';
import StatusBadge, { BoldRingIcon, RunningIcon } from './StatusBadge';
import ConfirmDialog from './ConfirmDialog';
import CronTaskDrawer, { jobToForm, templateToForm, type CronTaskFormValue } from './CronTaskDrawer';
import { resolveCronJobProjectName } from './cronProjectDisplay';
import { useClickOutside } from './useClickOutside';
import SimpleSelect from './SimpleSelect';
import { hasXiaoyiPushApiId, isCronTargetOptionDisabled } from './xiaoyiCronTarget';
import emptyIllustration from '../../assets/cron-empty.svg';
import { PageToolbarSearch } from '../ui';

// 任务列表分页：每页条数可选项（默认 20），纯前端本地分页——后端 cron.job.list 目前
// 一次性返回全部任务、不支持 offset/limit，见 bug002 progress.md 的方案说明
const PAGE_SIZE_OPTIONS = [10, 20, 50];
const DEFAULT_PAGE_SIZE = 20;

// 页码按钮列表：页数不多时全部展示，页数较多时只展示首页/尾页/当前页前后一页，其余用省略号折叠，
// 避免页数很多时（比如上百页）把一整行按钮撑爆
function buildPageList(current: number, total: number): (number | 'ellipsis')[] {
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }
  const pages: (number | 'ellipsis')[] = [1];
  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  if (start > 2) pages.push('ellipsis');
  for (let p = start; p <= end; p++) pages.push(p);
  if (end < total - 1) pages.push('ellipsis');
  pages.push(total);
  return pages;
}

interface PaginationBarProps {
  currentPage: number;
  totalPages: number;
  pageSize: number;
  totalCount: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

// 任务列表下方的分页条：每页条数下拉（10/20/50） + 当前范围提示 + 页码翻页。
// 下拉的弹出方向朝上（menuPlacement="up"）——分页条紧贴表格下方、离页面底部很近，向下弹出
// 经常需要用户再往下滚一屏才能看到选项，向上弹出正好贴着分页条本身展开，不用滚动。
// "每页显示"下拉始终展示（哪怕当前只有一页），因为它是用户对"每页看几条"的持久偏好，任务数
// 从多变少（比如筛出结果变少、任务被删除）不应该让这个控件也跟着消失，否则用户切到 50
// 条/页后任务数又降回一页以内，就再也切不回 20 条了；只有"上一页/页码/下一页"这组纯粹为翻页
// 服务的控件，在只有一页（或没有数据）时才没有意义，按 totalPages > 1 单独控制显示。
function PaginationBar({
  currentPage,
  totalPages,
  pageSize,
  totalCount,
  onPageChange,
  onPageSizeChange,
}: PaginationBarProps) {
  const { t } = useTranslation();
  const pageSizeOptions = useMemo(() => PAGE_SIZE_OPTIONS.map((n) => ({ value: String(n), label: String(n) })), []);
  const pages = useMemo(() => buildPageList(currentPage, totalPages), [currentPage, totalPages]);
  const rangeStart = totalCount === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const rangeEnd = Math.min(currentPage * pageSize, totalCount);

  return (
    <div
      className="mt-3 flex flex-wrap items-center justify-between gap-3 text-sm text-text-muted"
      data-testid="cron-pagination"
    >
      <div className="flex items-center gap-2" data-testid="cron-simple-select-1">
        <span data-testid="cron-pagination-page-size-label">{t('cron.pagination.pageSize')}</span>
        <SimpleSelect
          value={String(pageSize)}
          onChange={(v) => onPageSizeChange(Number(v))}
          options={pageSizeOptions}
          className="w-20"
          menuPlacement="up"
        />
        <span data-testid="cron-pagination-range-info">
          {t('cron.pagination.rangeInfo', { start: rangeStart, end: rangeEnd, total: totalCount })}
        </span>
      </div>
      {totalPages > 1 && (
        <div className="flex items-center gap-1">
          <button
            type="button"
            disabled={currentPage <= 1}
            onClick={() => onPageChange(currentPage - 1)}
            aria-label={t('cron.pagination.prev') ?? undefined}
            data-testid="cron-pagination-prev-btn"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <ChevronLeft size={14} />
          </button>
          {pages.map((p, idx) =>
            p === 'ellipsis' ? (
              <span key={`ellipsis-${idx}`} className="px-1.5 text-text-muted">
                …
              </span>
            ) : (
              <button
                key={p}
                type="button"
                onClick={() => onPageChange(p)}
                data-testid="cron-pagination-page-btn"
                data-variant={p}
                className={`flex h-7 min-w-7 items-center justify-center rounded-md px-1.5 text-sm ${
                  p === currentPage
                    ? 'bg-cron-action font-bold text-cron-action-foreground'
                    : 'text-text hover:bg-bg-hover'
                }`}
              >
                {p}
              </button>
            ),
          )}
          <button
            type="button"
            disabled={currentPage >= totalPages}
            onClick={() => onPageChange(currentPage + 1)}
            aria-label={t('cron.pagination.next') ?? undefined}
            data-testid="cron-pagination-next-btn"
            className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-text hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

// 主动推荐自动维护的 job id（与后端 proactive_cron_sync.PROACTIVE_JOB_ID 一致）。
// 该 job 的整体开关由 config 的 proactive_recommendation.enabled 驱动（关则删除，不在列表里）；
// 面板上禁用停止/删除，编辑时仅 cron 表达式与时区可改，其余字段只读（沿用旧面板约束，见
// upstream 提交 59cf6de7）。
const PROACTIVE_AUTO_JOB_ID = 'proactive-tick-auto';

// 用于展示已有任务的推送频道（含历史数据可能存在的 wecom/wechat）
const KNOWN_TARGET_KEYS = ['web', 'tui', 'xiaoyi', 'feishu', 'dingtalk', 'whatsapp', 'wecom', 'wechat'];
// 创建/编辑时可选的推送频道：wecom/wechat 已被 upstream 下架（见提交 e12d1952、d57567e4），
// 不在下拉里出现，但已有数据仍按上面 KNOWN_TARGET_KEYS 正常展示
const SELECTABLE_TARGET_KEYS = ['web', 'tui', 'xiaoyi', 'feishu', 'dingtalk', 'whatsapp'];

// 执行历史目前只有"该功能即将上线"占位（等 backend-requests.md #1 的真实数据接口交付），
// 用户要求先不在界面上露出入口（tab + 行内"运行历史"菜单项），但保留代码，等后端接口交付后
// 把这个开关打开即可，不用再重写 UI
const CRON_HISTORY_UI_ENABLED = false;

interface CronPanelProps {
  sessionId: string;
  onCreateViaChat: (initialInputValue: string) => void;
  /**
   * 跳转到"触发的会话"，复用工作面板的会话导航逻辑（App.tsx 的 requestSessionNavigation）。
   * 传 Session 对象（如"触发的会话"列表里已有完整数据）或直接传 session_id 字符串
   * （如立即执行返回的 session_id，还没有完整 Session 数据）。
   */
  onSelectSession: (session: Session | string) => void;
}

type TabKey = 'list' | 'template' | 'history';

function TemplateIcon({ icon }: { icon: CronTemplateUI['icon'] }) {
  const Icon = icon === 'trend' ? TrendingUp : icon === 'newspaper' ? Newspaper : Briefcase;
  return (
    <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-accent-subtle text-accent">
      <Icon size={18} />
    </span>
  );
}

// 任务总数统计行旁边的小标签（运行中/已暂停 各多少个），样式复刻自阶段1 demo 的 StatPill
function StatPill({ icon, label, count }: { icon: React.ReactNode; label: string; count: number }) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs text-text">
      {icon}
      {label} {count}
    </span>
  );
}

// ─── 表格列宽布局 & 拖拽调整 ─────────────────────────────────────────────────
// 布局（table-layout: fixed + colgroup）：各列 width / minWidth 配置在组件内 columns 数组里
// （antd Column API 同名字段），colgroup 解析优先级 = 拖拽后宽度 > 配置 width > auto；
// project 只配 minWidth 不配 width——fixed 布局下无 width 的列吸收全部剩余空间（表格列的
// flex-1 等价物）；Actions 列无宽度配置、按内容自适应（渲染后量宽写回 colgroup，否则 auto
// 的它会跟 project 平分剩余空间）。表格最小宽度走 CSS 原生路径：project 表头内容的
// min-width（= 各列指定宽 + project 下限）参与 fixed 布局的原生最小宽度计算，容器不足时
// 表格原生撑开、外层横向滚动兜底，project 永远分得到 ≥130px 不会被压成 0。
// 拖拽对齐 antd resizable 表格：拖表头右缘调整列宽（热区跨边框居中），松手宽度即固定；
// 拖宽仅会话内有效（不持久化），刷新后恢复列配置默认值。
// 列标识与表头顺序一致
const RESIZABLE_COLS = ['name', 'project', 'schedule', 'status', 'timezone', 'channel'] as const;
type ResizableColKey = (typeof RESIZABLE_COLS)[number];

// 非 project / Actions 列的默认宽度与最小宽度（px），拖拽下限也是它
const MIN_COL_WIDTH = 130;
const DEFAULT_COL_WIDTH = 130;
interface ColState {
  width: number;
  hasResized: boolean;
}

type ColStates = Record<ResizableColKey, ColState>;

const DEFAULT_COL_STATE: ColStates = {
  name: { width: 0, hasResized: false },
  project: { width: 0, hasResized: false },
  schedule: { width: 0, hasResized: false },
  status: { width: 0, hasResized: false },
  timezone: { width: 0, hasResized: false },
  channel: { width: 0, hasResized: false },
};

// antd columns 式列配置：colgroup / 表头 / 单元格全部由一份数组驱动，加列、调宽度、改渲染只改这里
interface CronColumnDef {
  /** 列标识：可调列用 RESIZABLE_COLS 里的 key，最后一列固定 'actions'（不参与调整、按内容自适应） */
  key: ResizableColKey | 'actions';
  /** 表头文案 i18n key */
  titleKey: string;
  /** 列宽度（px）：拖拽后的会话内宽度优先于它；不传时兜底 minWidth。不要指望 auto 列——
   *  colgroup 刻意不产出 auto（防 1px 假溢出，见 colgroup 处注释），自适应由表格 w-full
   *  的按比例分摊承担：想要某列更宽就给更大的 width，宽屏上比例自动放大 */
  width?: number;
  /** 最小列宽度（px）：拖拽下限 + 未配 width 时的兜底列宽；缺省回落到 MIN_COL_WIDTH */
  minWidth?: number;
  /** <td> 上的 data-testid；testid 挂在更内层元素上的列（name / actions）不传 */
  tdTestId?: string;
  /** 悬停提示（跟随行数据）；不需要的列不传 */
  tdTitle?: (job: CronTaskUI) => string | undefined;
  /** <td> 的 className（各列内容形态不同，逐一显式声明） */
  tdClassName: string;
  /** 单元格渲染 */
  render: (job: CronTaskUI) => React.ReactNode;
}

function Th({
  children,
  first,
  colKey,
  onResizeStart,
  resizing,
}: {
  children: React.ReactNode;
  first?: boolean;
  colKey?: ResizableColKey;
  onResizeStart?: (e: React.MouseEvent) => void;
  resizing?: boolean;
}) {
  return (
    <th className="group/th relative py-3 font-medium" data-testid={colKey ? `cron-th-${colKey}` : undefined}>
      {/* max-w-full + truncate：fixed 布局下列宽再窄，表头文字也只单行省略号，绝不换行撑高表头 */}
      <span className={`inline-block max-w-full truncate ${first ? 'px-4' : 'border-l border-border pl-4 pr-4'}`}>
        {children}
      </span>
      {/* 拖拽热区跨列边框居中（antd resizable 同款：宽 16px、向右越界 8px，命中更容易） */}
      {colKey && (
        <div
          onMouseDown={onResizeStart}
          data-testid={`cron-th-resize-${colKey}`}
          className="group/handle absolute inset-y-0 -right-2 z-10 w-4 cursor-col-resize select-none"
        >
          <span
            className={`absolute inset-y-0 left-1/2 w-0.5 -translate-x-1/2 bg-border transition-opacity ${
              resizing ? 'opacity-100' : 'opacity-0 group-hover/handle:opacity-100'
            }`}
          />
        </div>
      )}
    </th>
  );
}

function cronJobToUI(job: CronJobDTO, projects: ProjectInfo[]): CronTaskUI {
  // 空串与 default/default_code 统一按「未选真实项目」显示 "-"（Issue #2653 / bug009）。
  const projectName = resolveCronJobProjectName(job.project_id, projects);
  return {
    id: job.id,
    name: job.name,
    projectId: job.project_id,
    projectName,
    description: job.description,
    modelName: job.model_name ?? null,
    // 统一识别后端规范模式和历史别名，表单只展示 agent / team 两态。
    mode: isTeamCronModeValue(job.mode) ? 'team' : 'agent',
    cronExpr: job.cron_expr,
    timezone: job.timezone,
    wakeOffsetSeconds: normalizeWakeOffsetSeconds(job.wake_offset_seconds),
    enabled: job.enabled,
    expired: job.expired,
    deliveryChannel: job.targets,
  };
}

type StatusFilterKey = 'running' | 'paused' | 'expired';

// 判断某个 job 属于"运行中/已暂停/过期"哪一态，跟顶部统计 StatPill 的口径保持一致（expired 优先于
// enabled）；"运行状态"筛选下拉和统计计数共用这一个函数，避免筛选结果跟顶部数字对不上
function jobStatusKey(job: CronTaskUI): StatusFilterKey {
  if (job.expired) return 'expired';
  return job.enabled ? 'running' : 'paused';
}

// "运行状态"筛选下拉里每个选项要显示的视觉标志，直接复用表格"运行状态"列本来就在用的
// StatusBadge（同一份图标/颜色），不重新发明一套新样式；enabled/expired 这两个 props 反推自
// jobStatusKey 的三态定义，跟表格里的展示口径保持一致
const STATUS_FILTER_BADGE_PROPS: Record<StatusFilterKey, { enabled: boolean; expired: boolean }> = {
  running: { enabled: true, expired: false },
  paused: { enabled: false, expired: false },
  expired: { enabled: false, expired: true },
};

export default function CronPanel({ sessionId, onCreateViaChat, onSelectSession }: CronPanelProps) {
  const { t } = useTranslation();
  // 工作面板侧边栏的"按项目分组展示定时任务"用的是独立的 useCronStore（见
  // multi-session/sidebar/ConversationSidebar.tsx），跟这个面板自己的 jobs state 是两份数据；
  // 在这里创建/编辑/停止/删除任务后也要通知它刷新，否则侧边栏那边的任务文件夹会显示过期数据
  const reloadCronStore = useCronStore((s) => s.reload);
  const loadCronSessions = useCronStore((s) => s.loadCronSessions);

  const [jobs, setJobs] = useState<CronTaskUI[]>([]);
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [enabledChannels, setEnabledChannels] = useState<Set<string>>(new Set());
  // 小艺推送依赖 api_id；频道已注册但未配 api_id 时仍应置灰（Issue #2497）
  const [xiaoyiPushReady, setXiaoyiPushReady] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [activeTab, setActiveTab] = useState<TabKey>('list');
  const [search, setSearch] = useState('');

  // 任务列表分页状态：纯前端本地分页（见文件顶部 PAGE_SIZE_OPTIONS 注释）
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [currentPage, setCurrentPage] = useState(1);
  const tableWrapperRef = useRef<HTMLDivElement>(null);

  // "运行状态"筛选（下拉多选）：空集合 = 不筛选（显示全部），跟名称搜索是 AND 关系，
  // 只在任务列表 tab 展示这个筛选器（模板/执行历史没有"运行状态"这个概念）
  const [selectedStatuses, setSelectedStatuses] = useState<Set<StatusFilterKey>>(new Set());
  const [statusFilterOpen, setStatusFilterOpen] = useState(false);
  const statusFilterRef = useRef<HTMLDivElement>(null);
  useClickOutside(statusFilterRef, statusFilterOpen, () => setStatusFilterOpen(false));

  const [createMenuOpen, setCreateMenuOpen] = useState(false);
  const createMenuRef = useRef<HTMLDivElement>(null);
  useClickOutside(createMenuRef, createMenuOpen, () => setCreateMenuOpen(false));

  const [rowMenuJobId, setRowMenuJobId] = useState<string | null>(null);
  const [rowMenuAnchor, setRowMenuAnchor] = useState<DOMRect | null>(null);
  const [rowMenuDirection, setRowMenuDirection] = useState<'up' | 'down'>('down');
  const rowMenuRef = useRef<HTMLDivElement>(null);
  const rowMenuPortalRef = useRef<HTMLDivElement>(null);
  // "触发的会话" / "预览"弹层：从"更多"菜单里的按钮打开，菜单关闭后弹层独立存在。
  // 与"更多"菜单一样 portal 到 body（fixed 定位）：absolute 弹层会被 overflow-x-auto 的
  // 表格容器裁剪（表格底部一行 + 横向滚动时必现）。锚点复用"更多"按钮的 rect，
  // 菜单 → 弹层切换时位置不跳变。
  const [sessionsPopoverJobId, setSessionsPopoverJobId] = useState<string | null>(null);
  const [sessionsPopoverAnchor, setSessionsPopoverAnchor] = useState<DOMRect | null>(null);
  const [sessionsPopoverDirection, setSessionsPopoverDirection] = useState<'up' | 'down'>('down');
  const sessionsPopoverPortalRef = useRef<HTMLDivElement>(null);
  const [previewPopoverJobId, setPreviewPopoverJobId] = useState<string | null>(null);
  const [previewPopoverAnchor, setPreviewPopoverAnchor] = useState<DOMRect | null>(null);
  const [previewPopoverDirection, setPreviewPopoverDirection] = useState<'up' | 'down'>('down');
  const previewPopoverPortalRef = useRef<HTMLDivElement>(null);
  const closeRowMenu = useCallback(() => {
    setRowMenuJobId(null);
    setRowMenuAnchor(null);
  }, []);
  const closeSessionsPopover = useCallback(() => {
    setSessionsPopoverJobId(null);
    setSessionsPopoverAnchor(null);
  }, []);
  const closePreviewPopover = useCallback(() => {
    setPreviewPopoverJobId(null);
    setPreviewPopoverAnchor(null);
  }, []);
  const closeRowPopovers = useCallback(() => {
    closeRowMenu();
    closeSessionsPopover();
    closePreviewPopover();
  }, [closeRowMenu, closeSessionsPopover, closePreviewPopover]);
  // "更多"菜单 portal 到 body 后不在 rowMenuRef 子树里，不能再用 useClickOutside
  // （会把"点选项"误判成"点外面"，选项 onClick 还没触发菜单就卸载）。这里自己挂
  // pointerdown，同时判定触发器与 portal 菜单两个 ref，与 ModeSelector
  // （CronPanel/ModeSelector.tsx）同一套口径。两个弹层是纯 portal，判 portal ref 即可。
  useEffect(() => {
    if (rowMenuJobId === null) return;
    const handlePointerDown = (e: PointerEvent) => {
      if (!rowMenuRef.current?.contains(e.target as Node) && !rowMenuPortalRef.current?.contains(e.target as Node)) {
        closeRowMenu();
      }
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [rowMenuJobId, closeRowMenu]);
  useEffect(() => {
    if (sessionsPopoverJobId === null) return;
    const handlePointerDown = (e: PointerEvent) => {
      if (!sessionsPopoverPortalRef.current?.contains(e.target as Node)) closeSessionsPopover();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [sessionsPopoverJobId, closeSessionsPopover]);
  useEffect(() => {
    if (previewPopoverJobId === null) return;
    const handlePointerDown = (e: PointerEvent) => {
      if (!previewPopoverPortalRef.current?.contains(e.target as Node)) closePreviewPopover();
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [previewPopoverJobId, closePreviewPopover]);
  const [triggeredSessions, setTriggeredSessions] = useState<Record<string, Session[]>>({});
  const [triggeredSessionsLoading, setTriggeredSessionsLoading] = useState<Record<string, boolean>>({});

  // "预览"（接下来几次触发时间）弹层：功能在旧版 CronPanel 里有、阶段4重写时漏做了，
  // 后端 cron.job.preview 接口一直都在，这次顺手加回来，跟"触发的会话"同一套弹层模式
  const [previewRuns, setPreviewRuns] = useState<Record<string, { wake_at: string; push_at: string }[]>>({});
  const [previewLoading, setPreviewLoading] = useState<Record<string, boolean>>({});

  const [drawer, setDrawer] = useState<
    | { mode: 'create' | 'template'; initial?: CronTaskFormValue }
    | { mode: 'edit'; initial: CronTaskFormValue; jobId: string }
    | null
  >(null);

  const [confirmState, setConfirmState] = useState<{ type: 'delete' | 'stop' | 'runNow'; job: CronTaskUI } | null>(
    null,
  );
  const [confirmBusy, setConfirmBusy] = useState(false);

  // 列宽调整状态：仅会话内有效，不持久化（刷新后恢复列配置默认值）
  const [colStates, setColStates] = useState<ColStates>(() => ({ ...DEFAULT_COL_STATE }));
  const [resizingColKey, setResizingColKey] = useState<ResizableColKey | null>(null);
  const resizingCol = useRef<{ col: ResizableColKey; startX: number; startWidth: number } | null>(null);
  const tableRef = useRef<HTMLTableElement>(null);

  // Actions 列宽测量：fixed 布局下 auto 列会被平分剩余空间，需要量出按钮行实际内容宽度
  // （w-max 使其不受列宽影响）后以显式 px 写回 colgroup。不设依赖数组：每次渲染都校准一次
  // （语言切换等导致按钮文案宽度变化时自动跟随），宽度未变时 setState 会自行 bail out。
  // ceil 而非 round：div 是 w-max 不随列宽收缩，ceil 保证列宽 ≥ 内容实际宽度，杜绝
  // 亚像素部分向上传播成 1px 假溢出；bail-out 只容忍"变窄"方向的 1px（列比内容宽不会
  // 溢出），变宽必须立刻跟上，否则 w-max 内容会重新溢出列
  const [actionsWidth, setActionsWidth] = useState<number | null>(null);
  useLayoutEffect(() => {
    const div = tableRef.current?.querySelector<HTMLElement>('[data-testid="cron-job-actions"]');
    if (!div) return;
    const w = Math.ceil(div.getBoundingClientRect().width);
    setActionsWidth((prev) => (prev != null && w <= prev && prev - w <= 1 ? prev : w));
  });

  // 列宽拖拽：表格自首次渲染起就是 fixed 布局、所有列均有显式宽度，拖拽只改目标列。
  // 宽容器下表格 w-full 会把剩余空间按比例分摊到各列（见 colgroup 处注释），列的渲染宽
  // ≠ 配置宽，因此 mousedown 一律按当前渲染宽落定为显式宽度，否则拖拽第一步会跳回配置宽。
  // 拖拽下限取列配置的 minWidth（缺省回落 MIN_COL_WIDTH）；拖宽仅会话内有效，不持久化
  const handleResizeStart = useCallback(
    (colDef: CronColumnDef) => (e: React.MouseEvent) => {
      e.preventDefault();
      if (colDef.key === 'actions') return;
      const col = colDef.key;
      const minWidth = colDef.minWidth ?? MIN_COL_WIDTH;
      const th = tableRef.current?.querySelector<HTMLElement>(`[data-testid="cron-th-${col}"]`);
      const rendered = th ? Math.ceil(th.getBoundingClientRect().width) : 0;
      const configured =
        colStates[col].hasResized && colStates[col].width > 0
          ? colStates[col].width
          : (colDef.width ?? minWidth);
      const startWidth = Math.max(minWidth, rendered > 0 ? rendered : configured);
      if (!(colStates[col].hasResized && colStates[col].width === startWidth)) {
        setColStates((prev) => ({ ...prev, [col]: { width: startWidth, hasResized: true } }));
      }
      resizingCol.current = { col, startX: e.clientX, startWidth };
      setResizingColKey(col);

      const onMove = (move: MouseEvent) => {
        if (!resizingCol.current) return;
        const delta = move.clientX - resizingCol.current.startX;
        const newW = Math.max(minWidth, resizingCol.current.startWidth + delta);
        setColStates((prev) => {
          if (prev[col].width === newW) return prev;
          return { ...prev, [col]: { ...prev[col], width: newW, hasResized: true } };
        });
      };

      const onUp = () => {
        resizingCol.current = null;
        setResizingColKey(null);
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
      };

      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'col-resize';
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    },
    [colStates],
  );

  const channelLabel = useCallback(
    (targets: string) => (KNOWN_TARGET_KEYS.includes(targets) ? t(`cron.targets.${targets}`) : targets),
    [t],
  );

  // "计划于"列：能识别成 周期/按间隔/单次 六种模式之一的就转成人话摘要（"每天 09:30"），
  // 识别不了的（Agent工具/TUI建的、或手写 Cron表达式 tab 的任意表达式）原样展示 7 段式原文兜底
  const scheduleLabel = useCallback(
    (cronExpr: string) => {
      const parsed = cronExprToSchedule(cronExpr);
      return parsed ? summarizeSchedule(parsed, t) : <span className="mono">{cronExpr}</span>;
    },
    [t],
  );

  // silent=true 用于轮询/可见性刷新等后台静默拉取：不切 loading 态、失败时不清空现有列表、
  // 不弹错误提示，避免偶发网络抖动打断用户正在看的内容（见 bug007/bug008/bug009 progress.md）
  const loadJobs = useCallback(
    async (projectList: ProjectInfo[], options?: { silent?: boolean }) => {
      const silent = options?.silent ?? false;
      if (!silent) {
        setLoading(true);
        setError(null);
      }
      try {
        const payload = await webRequest<{ jobs: CronJobDTO[] }>('cron.job.list');
        // 展示所有渠道的定时任务（含飞书/钉钉等非 web 渠道创建的），不再按 targets 过滤隐藏；
        // 来源渠道由列表"渠道"列的 channelLabel 用 badge 形式标明，让用户一眼区分任务归属哪个渠道
        // （见 bug007/bug008/bug009 progress.md：此前 isWebChannelJob 过滤把非 web 任务藏掉，
        // 导致飞书建的任务在 web 列表永不出现，轮询再勤也无济于事）
        const allJobs = payload.jobs || [];
        setJobs(allJobs.map((j) => cronJobToUI(j, projectList)));
      } catch (loadError) {
        if (silent) {
          return;
        }
        const message = loadError instanceof Error ? loadError.message : t('cron.errors.loadJobs');
        setError(message);
        setJobs([]);
      } finally {
        if (!silent) {
          setLoading(false);
        }
      }
    },
    [t],
  );

  const loadProjects = useCallback(async () => {
    try {
      const payload = await projectRegistryClient.list('all');
      const visible = payload.projects || [];
      setProjects(visible);
      return visible;
    } catch {
      return [];
    }
  }, []);

  // 按已启用频道决定推送下拉可选项；小艺额外要求 api_id 已配置（Issue #2497）
  const loadChannels = useCallback(async () => {
    try {
      const payload = await webRequest<{ channels?: unknown[] }>('channel.get');
      const channels = payload?.channels || [];
      const enabled = new Set<string>();
      for (const item of channels) {
        if (item && typeof item === 'object' && 'channel_id' in item) {
          const channelId = (item as { channel_id: unknown }).channel_id;
          if (typeof channelId === 'string' && channelId.trim()) {
            enabled.add(channelId.trim().toLowerCase());
          }
        }
      }
      setEnabledChannels(enabled);
    } catch {
      // 忽略错误，保持空集合（下拉里全部选项禁用，用户仍可看到但选不了，不阻塞其他功能）
      setEnabledChannels(new Set());
    }

    try {
      const xiaoyiPayload = await webRequest<{ config?: unknown }>('channel.xiaoyi.get_conf');
      setXiaoyiPushReady(hasXiaoyiPushApiId(xiaoyiPayload?.config));
    } catch {
      // 拉不到小艺配置时保守置为不可用，避免无 api_id 仍可选
      setXiaoyiPushReady(false);
    }
  }, []);

  const targetOptions = useMemo(
    () =>
      SELECTABLE_TARGET_KEYS.map((id) => ({
        value: id,
        label: t(`cron.targets.${id}`),
        disabled: isCronTargetOptionDisabled(id, enabledChannels, xiaoyiPushReady),
      })),
    [enabledChannels, t, xiaoyiPushReady],
  );

  useEffect(() => {
    void (async () => {
      const projectList = await loadProjects();
      await loadJobs(projectList);
      await loadChannels();
    })();
  }, [loadChannels, loadJobs, loadProjects]);

  // 供轮询/可见性刷新的静默重拉使用：始终指向最新的 projects，避免定时器闭包拿到挂载时
  // 的旧值（projects 是异步加载的，轮询早于它变化时也不该被锁死在空数组上）
  const projectsRef = useRef<ProjectInfo[]>(projects);
  useEffect(() => {
    projectsRef.current = projects;
  }, [projects]);

  // 定时任务列表除了"挂载时拉一次"、"本页面自己发起的创建/编辑/启停/删除"、"同一 web 会话内
  // cron_ 前缀工具调用完成"这三种触发方式外，没有别的刷新入口——跨渠道（飞书/钉钉等）创建的
  // 任务，以及纯粹随时间推移产生的"过期"状态变化，都覆盖不到，只能等用户手动切页重新挂载
  // 才会看到最新数据（见 bug007/bug008/bug009 的根因分析，progress.md）。这里加一个 5 秒
  // 轮询兜底 + 页面重新可见时立即刷新一次，两者都走 silent 静默拉取，不影响 loading/error 展示。
  useEffect(() => {
    const silentReload = () => {
      void loadJobs(projectsRef.current, { silent: true });
    };
    const intervalId = window.setInterval(silentReload, 5000);
    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        silentReload();
      }
    };
    document.addEventListener('visibilitychange', onVisibilityChange);
    return () => {
      window.clearInterval(intervalId);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [loadJobs]);

  // 监听 Agent 工具调用结果：cron_ 前缀的工具（比如通过聊天创建/改动定时任务）执行完后
  // 自动刷新任务列表，不用用户手动刷新页面（复用的是 upstream 同款监听逻辑，见 progress.md）
  useEffect(() => {
    const CRON_TOOL_PREFIX = 'cron_';
    const unsubscribe = webClient.on('chat.tool_result', (event) => {
      const payload = event.payload as Record<string, unknown>;
      const inner = (payload?.tool_result as Record<string, unknown>) ?? payload;
      const toolName = String(inner?.tool_name ?? inner?.name ?? '');
      if (toolName === 'cron' || toolName.startsWith(CRON_TOOL_PREFIX)) {
        void loadJobs(projects);
        void reloadCronStore();
      }
    });
    return unsubscribe;
  }, [loadJobs, projects, reloadCronStore]);

  useEffect(() => {
    if (!success) return;
    const timer = window.setTimeout(() => setSuccess(null), 2000);
    return () => window.clearTimeout(timer);
  }, [success]);

  useEffect(() => {
    if (!error) return;
    const timer = window.setTimeout(() => setError(null), 2000);
    return () => window.clearTimeout(timer);
  }, [error]);

  const filteredJobs = useMemo(
    () =>
      jobs.filter(
        (j) =>
          j.name.toLowerCase().includes(search.trim().toLowerCase()) &&
          (selectedStatuses.size === 0 || selectedStatuses.has(jobStatusKey(j))),
      ),
    [jobs, search, selectedStatuses],
  );

  // 搜索内容变化时重置回第 1 页，避免搜索结果变少后停留在一个已经越界的页码上看到空白
  useEffect(() => {
    setCurrentPage(1);
  }, [search]);

  const totalPages = useMemo(
    () => Math.max(1, Math.ceil(filteredJobs.length / pageSize)),
    [filteredJobs.length, pageSize],
  );

  // 任务被删除/停止等操作导致 filteredJobs 变短时，当前页码也可能越界，钳制回合法范围
  useEffect(() => {
    setCurrentPage((p) => (p > totalPages ? totalPages : p));
  }, [totalPages]);

  const paginatedJobs = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredJobs.slice(start, start + pageSize);
  }, [filteredJobs, currentPage, pageSize]);

  // 翻页后把表格滚动回可视区域顶部，避免用户翻页后还停留在上次的滚动位置看不到新一页内容
  const goToPage = useCallback((page: number) => {
    setCurrentPage(page);
    tableWrapperRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  const changePageSize = useCallback((size: number) => {
    setPageSize(size);
    setCurrentPage(1);
  }, []);

  const filteredTemplates = useMemo(
    () => CRON_TEMPLATES.filter((tpl) => t(tpl.titleKey).toLowerCase().includes(search.trim().toLowerCase())),
    [search, t],
  );

  function toggleStatusFilter(key: StatusFilterKey) {
    setSelectedStatuses((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // 任务总数统计行旁边的分类计数：跟 StatusBadge 的判断逻辑保持一致（expired 优先于 enabled），
  // 也是"运行状态"筛选下拉复用的同一套口径（jobStatusKey）。运行中/已暂停/过期是任务本身状态的
  // 完整三态，不依赖后端；"运行失败"是执行历史维度的概念（某一次执行的结果），不属于这里，见
  // StatusBadge.tsx 顶部注释
  const runningCount = useMemo(() => jobs.filter((j) => jobStatusKey(j) === 'running').length, [jobs]);
  const pausedCount = useMemo(() => jobs.filter((j) => jobStatusKey(j) === 'paused').length, [jobs]);
  const expiredCount = useMemo(() => jobs.filter((j) => jobStatusKey(j) === 'expired').length, [jobs]);

  async function handleCreateSubmit(value: CronTaskFormValue) {
    try {
      await webRequest<{ job: CronJobDTO }>('cron.job.create', {
        name: value.name.trim(),
        description: value.description.trim(),
        cron_expr: value.cronExpr.trim(),
        timezone: value.timezone,
        targets: value.targets.trim() || 'web',
        enabled: value.enabled,
        wake_offset_seconds: normalizeWakeOffsetSeconds(value.wakeOffsetSeconds),
        // 始终显式带上 project_dir（未选项目传空串），不能省略这个 key——后端
        // gateway/channel_manager/web/app_web_handlers.py 的 _cron_job_create 用
        // "key 是否存在"区分"用户显式选了默认项目"（key 存在、值为空串，不可覆盖）和
        // "调用方未表达项目意图"（key 缺失，会从当前 WebSocket 会话的 project_dir 兜底填充）。
        // 手动创建抽屉这条链路用户明确看到并操作了"项目"下拉框，属于前一种情况；
        // 之前省略 key 会命中后端的会话兜底，导致"不选项目"被错误绑定成当前会话所在项目（bug003）。
        project_dir: value.projectDir ?? '',
        // 下拉框选中真实项目时一并带上 project_id：后端 controller.py resolve_cron_project_binding
        // 优先信任显式 project_id，只传 project_dir 需要多一层反查（见 CronTaskFormValue.projectId 注释）。
        // 未选项目（projectId 为 null）时不传这个 key，走 project_dir 空串的既有归默认项目逻辑。
        ...(value.projectId ? { project_id: value.projectId } : {}),
        // 一并带上 work_mode：AgentOS 多用户下 Gateway 不再本地反查项目表，work_mode
        // 需由前端（project.list 已含 work_mode）随 project_id 下发，保证归属/展示正确。
        ...(value.projectId && value.workMode ? { work_mode: value.workMode } : {}),
        ...(value.modelName ? { model_name: value.modelName } : {}),
        mode: value.mode,
        session_id: sessionId,
      });
      setSuccess(t('cron.success.created'));
      setDrawer(null);
      setActiveTab('list');
      await loadJobs(projects);
      // 新任务按 updated_at 倒序会排到列表最前面（见 gateway/cron/store.py 的 jobs.sort 排序规则），
      // 跳回第 1 页并滚到表格顶部，让用户直接看到刚创建的任务，不用自己翻页去找
      goToPage(1);
      void reloadCronStore();
    } catch (createError) {
      // 前端 cronExprValidation.ts 是逐字段本地校验，理论上仍可能漏判后端 croniter 实际支持/
      // 不支持的某种写法（见 bugfix 2026072401/bug010 第2轮分析：两边校验规则天然可能不同步）。
      // 这里把后端 cron.job.create 失败时返回的具体原因（webClient 已经把 WS 响应里的
      // error 字段原样放进 Error.message，见 services/webClient.ts resolvePending）拼进提示里，
      // 而不是只显示一句笼统的"创建任务失败"，这样即使前端校验漏放行了一条非法表达式，用户在
      // 提交时也能看到后端到底为什么拒绝，而不是无从下手。
      const reason = createError instanceof Error ? createError.message.trim() : '';
      const message = reason ? t('cron.errors.createFailedWithReason', { reason }) : t('cron.errors.createFailed');
      setError(message);
    }
  }

  async function handleEditSubmit(jobId: string, value: CronTaskFormValue) {
    try {
      const isProactive = jobId === PROACTIVE_AUTO_JOB_ID;
      // proactive 自动维护 job 只允许改 cron_expr 和 timezone；enabled/mode/name/description/
      // targets/model_name 由 Settings/cron_sync 管理，不能带，否则会跟 proactive.tick 的
      // 调度逻辑冲突（沿用 upstream 提交 e64dcf51/59cf6de7 的约束）。
      const patch = isProactive
        ? { cron_expr: value.cronExpr.trim(), timezone: value.timezone }
        : {
            name: value.name.trim(),
            description: value.description.trim(),
            cron_expr: value.cronExpr.trim(),
            timezone: value.timezone,
            targets: value.targets.trim() || 'web',
            enabled: value.enabled,
            wake_offset_seconds: normalizeWakeOffsetSeconds(value.wakeOffsetSeconds),
            ...(value.modelName ? { model_name: value.modelName } : {}),
            mode: value.mode,
          };
      await webRequest<{ job: CronJobDTO }>('cron.job.update', {
        id: jobId,
        patch,
        session_id: sessionId,
      });
      setSuccess(t('cron.success.updated'));
      setDrawer(null);
      await loadJobs(projects);
      // 编辑保存同样会刷新 updated_at、把任务顶到列表最前面，跳回第 1 页保持跟"新建"一致的体验
      goToPage(1);
      void reloadCronStore();
    } catch (updateError) {
      // 同 handleCreateSubmit：把后端 cron.job.update 失败时的具体原因透出，而不是只显示笼统的
      // "更新任务失败"（见 bugfix 2026072401/bug010 第2轮分析）。
      const reason = updateError instanceof Error ? updateError.message.trim() : '';
      const message = reason ? t('cron.errors.updateFailedWithReason', { reason }) : t('cron.errors.updateFailed');
      setError(message);
    }
  }

  async function handleStopConfirm() {
    if (!confirmState || confirmBusy) return;
    setConfirmBusy(true);
    try {
      await webRequest<{ job: CronJobDTO }>('cron.job.toggle', { id: confirmState.job.id, enabled: false });
      setSuccess(t('cron.success.statusUpdated'));
      await loadJobs(projects);
      // 停止也会经 store.update_job 刷新 updated_at、把任务顶到列表最前面，同"新建/编辑"一样跳回第 1 页
      goToPage(1);
      void reloadCronStore();
    } catch (toggleError) {
      const message = toggleError instanceof Error ? toggleError.message : t('cron.errors.toggleFailed');
      setError(message);
    } finally {
      setConfirmBusy(false);
      setConfirmState(null);
    }
  }

  // "启动"（恢复已暂停任务）是低风险操作，不需要像"停止"那样二次确认弹窗
  async function handleStart(job: CronTaskUI) {
    try {
      await webRequest<{ job: CronJobDTO }>('cron.job.toggle', { id: job.id, enabled: true });
      setSuccess(t('cron.success.statusUpdated'));
      await loadJobs(projects);
      // 同上：启动也会把任务顶到列表最前面，跳回第 1 页
      goToPage(1);
      void reloadCronStore();
    } catch (toggleError) {
      const message = toggleError instanceof Error ? toggleError.message : t('cron.errors.toggleFailed');
      setError(message);
    }
  }

  async function handleRunNowConfirm() {
    if (!confirmState || confirmBusy) return;
    setConfirmBusy(true);
    try {
      const result = await webRequest<{ accepted: boolean; run_id: string; session_id?: string }>('cron.job.run_now', {
        id: confirmState.job.id,
      });
      // proactive.tick 的"立即执行"不跳转：后端返回的 session_id 是 cron 执行会话
      // （cron_<ts>_<jobid>，空的），而推荐消息实际投递到 most_recent_active_session
      // （用户当前会话）。跳过去看到的是空欢迎页，推荐却在原会话——跳转无意义且打断用户。
      // 推荐消息会自然出现在用户当前会话里，无需主动跳转。
      const isProactiveJob = confirmState.job.id === PROACTIVE_AUTO_JOB_ID;
      if (result.session_id && !isProactiveJob) {
        useCronStore.getState().setLastRunSessionId(confirmState.job.id, result.session_id);
        onSelectSession(result.session_id);
      }
      setSuccess(t(isProactiveJob ? 'cron.success.proactiveRunNow' : 'cron.success.runNow'));
      // 刷新左侧栏该定时任务下展开的 session 列表（project.get_cron_sessions）
      const { id: cronId, projectId } = confirmState.job;
      if (cronId && projectId) {
        void loadCronSessions(projectId, cronId);
      }
    } catch (runNowError) {
      const message = runNowError instanceof Error ? runNowError.message : t('cron.errors.runNowFailed');
      setError(message);
    } finally {
      setConfirmBusy(false);
      setConfirmState(null);
    }
  }

  async function handleDeleteConfirm() {
    if (!confirmState || confirmBusy) return;
    setConfirmBusy(true);
    try {
      await webRequest<{ deleted: boolean }>('cron.job.delete', { id: confirmState.job.id });
      setSuccess(t('cron.success.deleted'));
      await loadJobs(projects);
      void reloadCronStore();
    } catch (deleteError) {
      const message = deleteError instanceof Error ? deleteError.message : t('cron.errors.deleteFailed');
      setError(message);
    } finally {
      setConfirmBusy(false);
      setConfirmState(null);
    }
  }

  function openTemplateDrawer(tpl: CronTemplateUI) {
    // 抽屉打开瞬间主动重拉一次项目列表：CronPanel 的 projects 只在挂载时拉取一次，
    // 停留页面期间新建的项目不会自动同步进来（bug003），这里保证每次打开抽屉都是最新数据
    void loadProjects();
    // 同步刷新推送频道可用性（含小艺 api_id），避免刚改完频道配置仍用旧置灰状态
    void loadChannels();
    setDrawer({ mode: 'template', initial: templateToForm(tpl, t(tpl.titleKey), t(tpl.descriptionKey)) });
  }

  // 弹层上下朝向：默认在锚点下方打开；下方余量放不下时改从锚点上方展开
  const resolvePopoverDirection = (rect: DOMRect, neededHeight: number): 'up' | 'down' =>
    window.innerHeight - rect.bottom >= neededHeight ? 'down' : 'up';

  // "触发的会话"：查这个定时任务名下有哪些会话（含手动/自动触发的执行），点了直接跳转过去。
  // 注意：定时任务真正执行时生成的会话 id 是 `cron_<ts>_<job.id>` 这种格式，不是正常聊天的
  // `sess_...`，工作面板目前只认 `sess_` 前缀的会话可以跳转（App.tsx 多处判断），这部分不是
  // 我们这次要修的范围——能跳的正常跳，跳不了的属于已知限制，等负责这块的同事处理。
  async function openSessionsPopover(job: CronTaskUI, anchorRect: DOMRect) {
    setSessionsPopoverDirection(resolvePopoverDirection(anchorRect, 300));
    setSessionsPopoverAnchor(anchorRect);
    closePreviewPopover();
    setSessionsPopoverJobId(job.id);
    setTriggeredSessionsLoading((prev) => ({ ...prev, [job.id]: true }));
    try {
      const payload = await projectRegistryClient.getCronSessions(job.projectId || 'default', job.id);
      setTriggeredSessions((prev) => ({ ...prev, [job.id]: payload.sessions || [] }));
    } catch {
      setTriggeredSessions((prev) => ({ ...prev, [job.id]: [] }));
    } finally {
      setTriggeredSessionsLoading((prev) => ({ ...prev, [job.id]: false }));
    }
  }

  async function openPreviewPopover(job: CronTaskUI, anchorRect: DOMRect) {
    setPreviewPopoverDirection(resolvePopoverDirection(anchorRect, 170));
    setPreviewPopoverAnchor(anchorRect);
    closeSessionsPopover();
    setPreviewPopoverJobId(job.id);
    setPreviewLoading((prev) => ({ ...prev, [job.id]: true }));
    try {
      const payload = await webRequest<{ next: { wake_at: string; push_at: string }[] }>('cron.job.preview', {
        id: job.id,
        count: 3,
        session_id: sessionId,
      });
      setPreviewRuns((prev) => ({ ...prev, [job.id]: payload.next || [] }));
    } catch (previewError) {
      const message = previewError instanceof Error ? previewError.message : t('cron.errors.previewFailed');
      setError(message);
      setPreviewRuns((prev) => ({ ...prev, [job.id]: [] }));
    } finally {
      setPreviewLoading((prev) => ({ ...prev, [job.id]: false }));
    }
  }

  function formatPreviewTime(value: string): string {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  // 列配置（antd columns 式，单一数据源）：colgroup / 表头 / 单元格全部由它 map 出来
  const columns: CronColumnDef[] = [
    {
      key: 'name',
      titleKey: 'cron.table.name',
      width: DEFAULT_COL_WIDTH,
      minWidth: MIN_COL_WIDTH,
      tdClassName: 'overflow-hidden px-4 py-3 text-text',
      render: (job) => {
        const isProactive = job.id === PROACTIVE_AUTO_JOB_ID;
        return (
          <div className="flex items-center gap-1">
            {/* 名称过长由列宽 + truncate 控制，colgroup 决定列宽；max-w 不再硬编码像素，
                min-w-0 让 flex 子项收缩到列宽而非将整列撑大。徽标 shrink-0 不被裁掉。 */}
            <span className="min-w-0 truncate" title={job.name} data-testid="cron-job-name">
              {job.name}
            </span>
            {isProactive && (
              <span
                className="inline-flex shrink-0 items-center rounded-full bg-cron-auto-managed-surface px-1.5 py-0.5 text-[10px] font-medium text-cron-auto-managed-text"
                title={t('cron.autoManagedHint') ?? undefined}
                data-testid="cron-job-auto-managed-badge"
              >
                {t('cron.autoManaged')}
              </span>
            )}
          </div>
        );
      },
    },
    {
      key: 'project',
      titleKey: 'cron.table.project',
      // 与其余列一致固定 130px：不再让 project 做唯一的 auto 列吸收剩余空间——auto 列的
      // min-content 会参与 fixed 布局的表格最小宽度计算，是容器宽度足够时仍冒出 1px 横向
      // 滚动条的来源；全部列显式定宽后 fixed 布局宽度完全确定，容器更宽时由各列均分拉伸
      width: DEFAULT_COL_WIDTH,
      minWidth: MIN_COL_WIDTH,
      tdTestId: 'cron-job-project',
      tdClassName: 'overflow-hidden truncate px-4 py-3 text-text',
      tdTitle: (job) => job.projectName ?? t('cron.table.noProject') ?? undefined,
      render: (job) => job.projectName ?? t('cron.table.noProject'),
    },
    {
      key: 'schedule',
      titleKey: 'cron.table.schedule',
      width: DEFAULT_COL_WIDTH,
      minWidth: MIN_COL_WIDTH,
      tdTestId: 'cron-job-schedule',
      tdClassName: 'overflow-hidden truncate px-4 py-3 text-text',
      render: (job) => scheduleLabel(job.cronExpr),
    },
    {
      key: 'status',
      titleKey: 'cron.table.status',
      width: DEFAULT_COL_WIDTH,
      minWidth: MIN_COL_WIDTH,
      tdClassName: 'px-4 py-3',
      render: (job) => {
        // proactive 自动维护 job 的整体开关由 config 控制（关了就删除，不在列表里），
        // 因此这里只有两态：过期 → 过期；否则 → 启用，不显示"禁用"中间态
        // （沿用 upstream 提交 59cf6de7 的约束）
        const isProactive = job.id === PROACTIVE_AUTO_JOB_ID;
        return <StatusBadge enabled={isProactive ? !job.expired : job.enabled} expired={job.expired} />;
      },
    },
    {
      key: 'timezone',
      titleKey: 'cron.table.timezone',
      width: DEFAULT_COL_WIDTH,
      minWidth: MIN_COL_WIDTH,
      tdTestId: 'cron-job-timezone',
      tdClassName: 'overflow-hidden truncate px-4 py-3 text-text',
      render: (job) => job.timezone,
    },
    {
      key: 'channel',
      titleKey: 'cron.table.channel',
      width: DEFAULT_COL_WIDTH,
      minWidth: MIN_COL_WIDTH,
      tdTestId: 'cron-job-channel',
      tdClassName: 'overflow-hidden truncate px-4 py-3 text-text',
      render: (job) => channelLabel(job.deliveryChannel),
    },
    {
      key: 'actions',
      titleKey: 'cron.table.actions',
      tdClassName: 'relative py-3',
      render: (job) => {
        const isProactive = job.id === PROACTIVE_AUTO_JOB_ID;
        return (
          <>
            {/* w-max + px-4：容器始终按「内容+内边距」单行宽度渲染，供 useLayoutEffect 量取
                Actions 列应有列宽（fixed 布局下 auto 的它否则会跟 project 平分剩余空间）；
                whitespace-nowrap：被挤压时按钮文字不许换行，行高必须稳定 */}
            <div
              className="flex w-max items-center gap-3 whitespace-nowrap px-4"
              data-testid="cron-job-actions"
              data-variant={job.id}
            >
              {/* proactive job 没有真正的"停止"态（enabled 由 config 驱动，不是用户可切的
                开关，同 StatusBadge 的 enabled 判断），立即执行的禁用条件不看它的 enabled */}
              {job.expired || (!isProactive && !job.enabled) ? (
                <span
                  className="text-sm text-text-muted/50 cursor-not-allowed select-none"
                  title={
                    t(job.expired ? 'cron.errors.expiredCannotRunNow' : 'cron.errors.disabledCannotRunNow') ?? undefined
                  }
                  data-testid="cron-job-run-now-btn"
                  data-variant="disabled"
                >
                  {t('cron.table.runNow')}
                </span>
              ) : (
                <button
                  onClick={() => setConfirmState({ type: 'runNow', job })}
                  data-testid="cron-job-run-now-btn"
                  data-variant="enabled"
                  className="text-sm text-cron-action-link hover:opacity-80"
                >
                  {t('cron.table.runNow')}
                </button>
              )}
              <button
                onClick={() => {
                  void loadChannels();
                  setDrawer({ mode: 'edit', initial: jobToForm(job), jobId: job.id });
                }}
                data-testid="cron-job-edit-btn"
                className="text-sm text-cron-action-link hover:opacity-80"
              >
                {t('cron.table.edit')}
              </button>
              {isProactive ? (
                <span
                  className="text-sm text-text-muted/50 cursor-not-allowed select-none"
                  title={t('cron.autoManagedToggleDisabled') ?? undefined}
                  data-testid="cron-job-stop-btn"
                  data-variant="disabled"
                >
                  {t('cron.table.stop')}
                </span>
              ) : job.expired ? (
                <span
                  className="text-sm text-text-muted/50 cursor-not-allowed select-none"
                  title={t('cron.errors.expiredCannotEnable') ?? undefined}
                  data-testid="cron-job-start-btn"
                  data-variant="disabled"
                >
                  {t('cron.table.start')}
                </span>
              ) : job.enabled ? (
                <button
                  onClick={() => setConfirmState({ type: 'stop', job })}
                  data-testid="cron-job-stop-btn"
                  data-variant="enabled"
                  className="text-sm text-cron-action-link hover:opacity-80"
                >
                  {t('cron.table.stop')}
                </button>
              ) : (
                <button
                  onClick={() => void handleStart(job)}
                  data-testid="cron-job-start-btn"
                  data-variant="enabled"
                  className="text-sm text-cron-action-link hover:opacity-80"
                >
                  {t('cron.table.start')}
                </button>
              )}
              <div ref={rowMenuJobId === job.id ? rowMenuRef : undefined}>
                <button
                  disabled={isProactive}
                  onClick={(e) => {
                    if (rowMenuJobId === job.id) {
                      closeRowMenu();
                      return;
                    }
                    const rect = e.currentTarget.getBoundingClientRect();
                    setRowMenuDirection(window.innerHeight - rect.bottom >= 150 ? 'down' : 'up');
                    setRowMenuAnchor(rect);
                    setRowMenuJobId(job.id);
                  }}
                  data-testid="cron-job-more-btn"
                  data-variant={isProactive ? 'disabled' : 'enabled'}
                  className={`flex items-center gap-0.5 text-sm ${isProactive ? 'cursor-not-allowed text-text-muted/50' : 'text-cron-action-link hover:opacity-80'}`}
                >
                  {t('cron.table.more')} <ChevronDown size={13} />
                </button>
                {!isProactive &&
                  rowMenuJobId === job.id &&
                  rowMenuAnchor &&
                  createPortal(
                    <div
                      ref={rowMenuPortalRef}
                      className="w-28 rounded-lg border border-border bg-card py-1.5 shadow-lg"
                      style={
                        rowMenuDirection === 'up'
                          ? {
                              position: 'fixed',
                              bottom: window.innerHeight - rowMenuAnchor.top + 4,
                              left: rowMenuAnchor.right,
                              transform: 'translateX(-100%)',
                              zIndex: 9999,
                            }
                          : {
                              position: 'fixed',
                              top: rowMenuAnchor.bottom + 4,
                              left: rowMenuAnchor.right,
                              transform: 'translateX(-100%)',
                              zIndex: 9999,
                            }
                      }
                      data-testid="cron-job-more-menu"
                    >
                      <button
                        onClick={() => {
                          // 菜单即将关闭，先取"更多"按钮的 rect 当弹层锚点（菜单定位用的同一 rect）
                          if (rowMenuAnchor) void openSessionsPopover(job, rowMenuAnchor);
                          closeRowMenu();
                        }}
                        data-testid="cron-job-more-triggered-sessions-btn"
                        className="block w-full px-3 py-2 text-left text-sm text-cron-action-link hover:bg-bg-hover"
                      >
                        {t('cron.table.triggeredSessions')}
                      </button>
                      <button
                        onClick={() => {
                          if (rowMenuAnchor) void openPreviewPopover(job, rowMenuAnchor);
                          closeRowMenu();
                        }}
                        data-testid="cron-job-more-preview-btn"
                        className="block w-full px-3 py-2 text-left text-sm text-cron-action-link hover:bg-bg-hover"
                      >
                        {t('cron.previewAction')}
                      </button>
                      {isProactive ? (
                        <span
                          className="block w-full px-3 py-2 text-left text-sm text-text-muted/50 cursor-not-allowed"
                          title={t('cron.autoManagedToggleDisabled') ?? undefined}
                          data-testid="cron-job-more-delete-btn"
                          data-variant="disabled"
                        >
                          {t('cron.delete')}
                        </span>
                      ) : (
                        <button
                          onClick={() => {
                            closeRowMenu();
                            setConfirmState({ type: 'delete', job });
                          }}
                          data-testid="cron-job-more-delete-btn"
                          data-variant="enabled"
                          className="block w-full px-3 py-2 text-left text-sm text-danger hover:bg-bg-hover"
                        >
                          {t('cron.delete')}
                        </button>
                      )}
                      {CRON_HISTORY_UI_ENABLED && (
                        <button
                          onClick={() => {
                            closeRowMenu();
                            setSuccess(t('cron.history.comingSoon'));
                          }}
                          data-testid="cron-job-more-history-btn"
                          className="block w-full px-3 py-2 text-left text-sm text-cron-action-link hover:bg-bg-hover"
                        >
                          {t('cron.table.history')}
                        </button>
                      )}
                    </div>,
                    document.body,
                  )}
              </div>
            </div>
            {sessionsPopoverJobId === job.id && sessionsPopoverAnchor &&
              createPortal(
                <div
                  ref={sessionsPopoverPortalRef}
                  className="w-64 rounded-lg border border-border bg-card py-1.5 shadow-lg"
                  style={
                    sessionsPopoverDirection === 'up'
                      ? {
                          position: 'fixed',
                          bottom: window.innerHeight - sessionsPopoverAnchor.top + 4,
                          left: sessionsPopoverAnchor.right,
                          transform: 'translateX(-100%)',
                          zIndex: 9999,
                        }
                      : {
                          position: 'fixed',
                          top: sessionsPopoverAnchor.bottom + 4,
                          left: sessionsPopoverAnchor.right,
                          transform: 'translateX(-100%)',
                          zIndex: 9999,
                        }
                  }
                  data-testid="cron-sessions-popover"
                >
                  <div
                    className="px-3 py-1.5 text-xs font-bold text-text-muted"
                    data-testid="cron-sessions-popover-title"
                  >
                    {t('cron.table.triggeredSessions')}
                  </div>
                  {triggeredSessionsLoading[job.id] && (
                    <div className="px-3 py-2 text-sm text-text-muted" data-testid="cron-sessions-popover-loading">
                      {t('common.loading')}
                    </div>
                  )}
                  {!triggeredSessionsLoading[job.id] && (triggeredSessions[job.id]?.length ?? 0) > 0 && (
                    <div className="max-h-64 overflow-y-auto">
                      {triggeredSessions[job.id].map((s) => (
                        <button
                          key={s.session_id}
                          onClick={() => {
                            closeSessionsPopover();
                            onSelectSession(s);
                          }}
                          className="block w-full truncate px-3 py-2 text-left text-sm text-text hover:bg-bg-hover"
                          title={s.title}
                          data-testid="cron-sessions-popover-item"
                          data-variant={s.session_id}
                        >
                          {s.title || s.session_id}
                        </button>
                      ))}
                    </div>
                  )}
                  {!triggeredSessionsLoading[job.id] && (triggeredSessions[job.id]?.length ?? 0) === 0 && (
                    <div className="px-3 py-2 text-sm text-text-muted" data-testid="cron-sessions-popover-empty">
                      {t('cron.table.noTriggeredSessions')}
                    </div>
                  )}
                </div>,
                document.body,
              )}
            {previewPopoverJobId === job.id && previewPopoverAnchor &&
              createPortal(
                <div
                  ref={previewPopoverPortalRef}
                  className="w-64 rounded-lg border border-border bg-card py-1.5 shadow-lg"
                  style={
                    previewPopoverDirection === 'up'
                      ? {
                          position: 'fixed',
                          bottom: window.innerHeight - previewPopoverAnchor.top + 4,
                          left: previewPopoverAnchor.right,
                          transform: 'translateX(-100%)',
                          zIndex: 9999,
                        }
                      : {
                          position: 'fixed',
                          top: previewPopoverAnchor.bottom + 4,
                          left: previewPopoverAnchor.right,
                          transform: 'translateX(-100%)',
                          zIndex: 9999,
                        }
                  }
                  data-testid="cron-preview-popover"
                >
                  <div
                    className="truncate px-3 py-1.5 text-xs font-bold text-text-muted"
                    title={job.name}
                    data-testid="cron-preview-popover-title"
                  >
                    {job.name}
                  </div>
                  {previewLoading[job.id] && (
                    <div className="px-3 py-2 text-sm text-text-muted" data-testid="cron-preview-popover-loading">
                      {t('cron.preview.loading')}
                    </div>
                  )}
                  {!previewLoading[job.id] && (previewRuns[job.id]?.length ?? 0) > 0 && (
                    <div className="px-3 py-2 text-xs text-text">
                      {previewRuns[job.id].map((item, index) => (
                        <div
                          key={`${job.id}-${index}`}
                          className="py-0.5"
                          data-testid="cron-preview-popover-run-item"
                          data-variant={index + 1}
                        >
                          {t('cron.preview.label', { index: index + 1 })}：{formatPreviewTime(item.push_at)}
                        </div>
                      ))}
                    </div>
                  )}
                  {!previewLoading[job.id] && (previewRuns[job.id]?.length ?? 0) === 0 && (
                    <div className="px-3 py-2 text-sm text-text-muted" data-testid="cron-preview-popover-empty">
                      {t('cron.preview.empty')}
                    </div>
                  )}
                </div>,
                document.body,
              )}
          </>
        );
      },
    },
  ];

  // 表格最小宽度不用 JS 计算（内联 minWidth 会参与滚动条相关的布局反馈，是 x 滚动条
  // 临界宽度抖动的根源）：全列在 colgroup 显式定宽（宽度解析见下，刻意不产出 auto 列），
  // 窄容器下 fixed 布局按列宽总和原生撑开表格、外层横向滚动兜底；宽容器下 w-full 把
  // 剩余空间按各列 width 比例分摊（等效 antd flex 列）

  return (
    // scrollbar-gutter:stable：竖向滚动条出现/消失不再挤压内容宽度。没有它，table 横向滚动条
    // 出现会撑高面板内容 → 面板竖向滚动条出现 → 内容宽度变窄 → 表格更贴 min-width → 横向滚动条
    // 翻转 → 高度又变 → 在临界宽度附近来回抖动（x 滚动条快速切换闪烁的根源）
    <div
      className="flex-1 min-h-0 relative overflow-y-auto [scrollbar-gutter:stable]"
      onScroll={closeRowPopovers}
      data-testid="cron-panel"
      data-session-id={sessionId}
    >
      {success && (
        <div
          className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 z-20"
          data-testid="cron-success-toast"
        >
          <div className="bg-ok px-4 py-2 text-sm text-text-inverse rounded-lg shadow-lg animate-rise">{success}</div>
        </div>
      )}
      {error && (
        <div
          className="pointer-events-none absolute top-3 left-1/2 -translate-x-1/2 z-20"
          data-testid="cron-error-toast"
        >
          <div className="bg-danger px-4 py-2 text-sm text-text-inverse rounded-lg shadow-lg animate-rise">{error}</div>
        </div>
      )}

      {/* 宽度跟随主窗口自适应（w-[90%]），但用 max-w 封顶避免超宽屏上被拉得过宽，
          两侧留白也不会随窗口变宽而无限增大 */}
      <div className="mx-auto flex w-[90%] max-w-[1600px] flex-col py-8 min-h-full">
        {/* 页头 */}
        <div className="flex items-start justify-between" data-testid="cron-page-header">
          <div>
            <h1 className="text-xl font-semibold text-text-strong" data-testid="cron-page-title">
              {t('cron.pageTitle')}
            </h1>
            <p className="mt-1 text-sm text-text-muted" data-testid="cron-page-subtitle">
              {t('cron.pageSubtitle')}
            </p>
          </div>
        </div>

        {/* Tab 导航 */}
        <div className="page-toolbar mb-6" data-testid="cron-tabs">
          <nav className="chat-picker-panel__tabs !mb-0" role="tablist" data-testid="cron-tab-list">
            {(
              [
                ['list', t('cron.tabs.list')],
                ['template', t('cron.tabs.template')],
                ...(CRON_HISTORY_UI_ENABLED ? [['history', t('cron.tabs.history')]] : []),
              ] as [TabKey, string][]
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={activeTab === key}
                onClick={() => setActiveTab(key)}
                data-testid="cron-tab"
                data-variant={key}
                className={activeTab === key ? 'is-active' : ''}
              >
                {label}
              </button>
            ))}
          </nav>
          <div className="relative flex min-w-0 flex-1 items-center justify-end gap-2" ref={createMenuRef}>
            {/* 搜索框 + 运行状态筛选（筛选下拉只在任务列表 tab 展示，模板/执行历史没有"运行状态"这个概念） */}
            {!(activeTab === 'list' && jobs.length === 0) && activeTab !== 'history' && (
              <>
                {activeTab === 'list' && (
                  <div className="relative shrink-0" ref={statusFilterRef}>
                    <button
                      onClick={() => setStatusFilterOpen((v) => !v)}
                      className={`flex items-center gap-1.5 rounded-md py-1.5 text-sm ${
                        selectedStatuses.size > 0 ? 'text-accent' : 'text-text'
                      } bg-card hover:text-accent`}
                      data-testid="cron-status-filter-toggle"
                    >
                      {t('cron.table.status')}
                      {selectedStatuses.size > 0 && (
                        <span
                          className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[10px] font-bold text-text-inverse"
                          data-testid="cron-status-filter-count"
                        >
                          {selectedStatuses.size}
                        </span>
                      )}
                      <ChevronDown size={14} />
                    </button>
                    {statusFilterOpen && (
                      <div className="dropdown-menu w-40" data-testid="cron-status-filter-menu">
                        {(['running', 'paused', 'expired'] as const).map((key) => (
                          <label
                            key={key}
                            className="flex cursor-pointer items-center gap-2 px-3 py-1.5 text-sm text-text hover:bg-bg-hover"
                            data-testid="cron-status-filter-option"
                            data-variant={key}
                          >
                            <input
                              type="checkbox"
                              checked={selectedStatuses.has(key)}
                              onChange={() => toggleStatusFilter(key)}
                              data-testid="cron-status-filter-checkbox"
                              data-variant={key}
                              className="h-3.5 w-3.5 rounded border-border"
                            />
                            <StatusBadge
                              enabled={STATUS_FILTER_BADGE_PROPS[key].enabled}
                              expired={STATUS_FILTER_BADGE_PROPS[key].expired}
                            />
                          </label>
                        ))}
                        {selectedStatuses.size > 0 && (
                          <button
                            onClick={() => setSelectedStatuses(new Set())}
                            data-testid="cron-status-filter-reset-btn"
                            className="mt-1 block w-full border-t border-border px-3 pt-2 text-left text-xs text-text-muted hover:text-text"
                          >
                            {t('cron.filter.reset')}
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                )}
                <PageToolbarSearch
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  onClear={() => setSearch('')}
                  placeholder={t('cron.search.placeholder') ?? undefined}
                  wrapperTestId="cron-search"
                  inputTestId="cron-search-input"
                  measureSelector='[data-testid="cron-panel"]'
                />
              </>
            )}
            <button
              onClick={() => setCreateMenuOpen((v) => !v)}
              className="flex items-center gap-2 rounded-full bg-cron-action px-6 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover"
              data-testid="cron-create-toggle"
            >
              {t('cron.createMenu.trigger')} <ChevronDown size={14} />
            </button>
            {createMenuOpen && (
              <div className="dropdown-menu" data-testid="cron-create-menu">
                <button
                  onClick={() => {
                    setCreateMenuOpen(false);
                    void loadProjects();
                    void loadChannels();
                    setDrawer({ mode: 'create' });
                  }}
                  data-testid="cron-create-menu-manual-btn"
                  className="dropdown-menu-item"
                >
                  {t('cron.createMenu.manual')}
                </button>
                <button
                  onClick={() => {
                    setCreateMenuOpen(false);
                    onCreateViaChat(t('cron.createMenu.viaChatPrompt'));
                  }}
                  data-testid="cron-create-menu-via-chat-btn"
                  className="dropdown-menu-item"
                >
                  {t('cron.createMenu.viaChat')}
                </button>
              </div>
            )}
          </div>
        </div>

        {/* 任务总数统计行；任务列表/执行历史都显示，"任务总数"字号跟任务列表表格正文一致（text-sm），
            不用 text-lg 那么突出。任务列表额外带运行中/已暂停/过期三个分类计数（StatPill，复刻自
            阶段1 demo）——这是任务本身状态的完整三态，不依赖后端。"运行失败"属于执行历史维度
            （某一次执行的结果），不属于任务列表，不会出现在这里，见 backend-requests.md #1。
            执行历史目前没有真实执行记录数据（tab 本身也被 CRON_HISTORY_UI_ENABLED 隐藏），先只保留
            总数展示，不编造假的分类计数。空状态页面不显示这一行 */}
        {(activeTab === 'list' || activeTab === 'history') && jobs.length > 0 && (
          <div className="mb-4 flex items-center gap-3" data-testid="cron-stats-row">
            <span className="text-sm font-bold text-text-strong" data-testid="cron-stats-total">
              {t('cron.stats.total', { count: jobs.length })}
            </span>
            {activeTab === 'list' && (
              <>
                <StatPill
                  icon={
                    <span className="text-cron-running">
                      <RunningIcon size={15} />
                    </span>
                  }
                  label={t('cron.status.running')}
                  count={runningCount}
                />
                <StatPill
                  icon={
                    <span className="text-text-muted">
                      <BoldRingIcon />
                    </span>
                  }
                  label={t('cron.status.paused')}
                  count={pausedCount}
                />
                <StatPill
                  icon={
                    <span className="text-warn">
                      <BoldRingIcon />
                    </span>
                  }
                  label={t('cron.status.expired')}
                  count={expiredCount}
                />
              </>
            )}
          </div>
        )}

        {/* tab: 任务列表 */}
        {activeTab === 'list' && loading && (
          <div
            className="rounded-lg border border-border bg-secondary/30 px-3 py-4 flex items-center justify-center"
            data-testid="cron-loading"
          >
            {t('cron.loading')}
          </div>
        )}
        {activeTab === 'list' && !loading && jobs.length === 0 && (
          <div className="flex flex-1 flex-col items-center justify-center" data-testid="cron-empty">
            {/* 创建定时任务模块保持在可视区域垂直居中 */}
            <div className="flex-1 flex flex-col items-center justify-center gap-4">
              <img src={emptyIllustration} alt="" className="h-20 w-20" />
              <button
                onClick={() => {
                  // 同上：打开抽屉瞬间重拉一次项目列表与频道可用性
                  void loadProjects();
                  void loadChannels();
                  setDrawer({ mode: 'create' });
                }}
                data-testid="cron-empty-create-btn"
                className="btn !py-[5px] !px-4 !border-0 outline outline-1 outline-[var(--color-button-border)] rounded-[16px] hover:!transform-none"
              >
                {t('cron.empty.createButton')}
              </button>
            </div>
            {/* 任务模板模块沉到页面下方，不紧跟在创建按钮下面 */}
            <div className="w-full pb-4">
              <div className="mb-3 flex items-center justify-between">
                <span className="text-sm font-bold text-text-strong" data-testid="cron-empty-template-section-title">
                  {t('cron.empty.templateSectionTitle')}
                </span>
                <button
                  onClick={() => setActiveTab('template')}
                  data-testid="cron-empty-template-more-btn"
                  className="text-xs text-accent hover:text-accent-hover"
                >
                  {t('cron.empty.templateMore')}
                </button>
              </div>
              <div className="grid grid-cols-3 gap-4">
                {CRON_TEMPLATES.map((tpl) => (
                  <button
                    key={tpl.id}
                    onClick={() => openTemplateDrawer(tpl)}
                    data-testid="cron-empty-template-card"
                    data-variant={tpl.id}
                    className="cron-template-card rounded-lg border border-border bg-card p-4 text-left transition-colors hover:border-accent"
                  >
                    <div className="mb-2 flex items-center gap-2">
                      <TemplateIcon icon={tpl.icon} />
                      <span className="text-sm font-bold text-text-strong">{t(tpl.titleKey)}</span>
                    </div>
                    <p className="line-clamp-3 text-xs leading-relaxed text-text-muted">{t(tpl.descriptionKey)}</p>
                  </button>
                ))}
              </div>
            </div>
          </div>
        )}
        {activeTab === 'list' && !loading && jobs.length > 0 && filteredJobs.length === 0 && (
          <div
            className="flex min-h-[30vh] flex-col items-center justify-center gap-2 text-text-muted"
            data-testid="cron-search-no-results-jobs"
          >
            <p className="text-sm">{t('cron.search.noResultsJobs')}</p>
          </div>
        )}
        {activeTab === 'list' && !loading && jobs.length > 0 && filteredJobs.length > 0 && (
          <>
            <div
              ref={tableWrapperRef}
              className="overflow-x-auto rounded-lg border border-[var(--color-border-default)]"
              onScroll={closeRowPopovers}
              data-testid="cron-jobs-table"
            >
              <table ref={tableRef} className="w-full border-collapse text-sm" style={{ tableLayout: 'fixed' }}>
                <colgroup>
                  {columns.map((col) => {
                    if (col.key === 'actions') {
                      return (
                        <col key={col.key} style={{ width: actionsWidth != null ? `${actionsWidth}px` : 'auto' }} />
                      );
                    }
                    const s = colStates[col.key];
                    // 宽度解析：拖拽后的会话内宽度（>0 防御）> 配置 width > 兜底显式宽。
                    // 刻意不产出 auto 列（antd 式 flex 列语义由别处承担）：fixed 布局下 auto
                    // 列宽度来自「容器宽 - 显式列总和」的减法与分配舍入，高 DPI/亚像素下
                    // 可能把表格右缘撑出 ~1px 假溢出——空间充足也出 1px 横向滚动条的元凶
                    // （实测去掉 auto 列即消失）。自适应不需要 auto：全列显式定宽 + 表格
                    // w-full，宽容器下剩余空间按各列 width 比例分摊（等效 antd flex 列），
                    // 窄容器下按列宽总和撑开、外层横向滚动兜底；想要某列更宽就给更大 width
                    const w =
                      s.hasResized && s.width > 0
                        ? `${s.width}px`
                        : col.width != null
                          ? `${col.width}px`
                          : `${col.minWidth ?? DEFAULT_COL_WIDTH}px`;
                    return <col key={col.key} style={{ width: w }} />;
                  })}
                </colgroup>
                <thead>
                  <tr
                    className="border-b border-border bg-cron-table-header-surface text-left text-text"
                    data-testid="cron-jobs-table-header"
                  >
                    {columns.map((col, idx) => (
                      <Th
                        key={col.key}
                        first={idx === 0}
                        colKey={col.key === 'actions' ? undefined : col.key}
                        onResizeStart={col.key === 'actions' ? undefined : handleResizeStart(col)}
                        resizing={col.key !== 'actions' && resizingColKey === col.key}
                      >
                        {t(col.titleKey)}
                      </Th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {paginatedJobs.map((job) => (
                    <tr
                      key={job.id}
                      className="border-b border-border last:border-0"
                      data-testid="cron-job-row"
                      data-variant={job.id}
                    >
                      {columns.map((col) => (
                        <td
                          key={col.key}
                          className={col.tdClassName}
                          data-testid={col.tdTestId}
                          title={col.tdTitle?.(job)}
                        >
                          {col.render(job)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <PaginationBar
              currentPage={currentPage}
              totalPages={totalPages}
              pageSize={pageSize}
              totalCount={filteredJobs.length}
              onPageChange={goToPage}
              onPageSizeChange={changePageSize}
            />
          </>
        )}

        {/* tab: 任务模板 */}
        {activeTab === 'template' &&
          (filteredTemplates.length > 0 ? (
            <div className="grid grid-cols-3 gap-4" data-testid="cron-template-grid">
              {filteredTemplates.map((tpl) => (
                <button
                  key={tpl.id}
                  onClick={() => openTemplateDrawer(tpl)}
                  data-testid="cron-template-card"
                  data-variant={tpl.id}
                  className="cron-template-card rounded-lg border border-border bg-card p-4 text-left transition-colors hover:border-accent"
                >
                  <div className="mb-2 flex items-center gap-2">
                    <TemplateIcon icon={tpl.icon} />
                    <span className="text-sm font-bold text-text-strong" data-testid="cron-template-card-title">
                      {t(tpl.titleKey)}
                    </span>
                  </div>
                  <p className="text-xs leading-relaxed text-text-muted" data-testid="cron-template-card-description">
                    {t(tpl.descriptionKey)}
                  </p>
                </button>
              ))}
            </div>
          ) : (
            <div
              className="flex min-h-[30vh] flex-col items-center justify-center gap-2 text-text-muted"
              data-testid="cron-template-search-no-results"
            >
              <p className="text-sm">{t('cron.search.noResults')}</p>
            </div>
          ))}

        {/* tab: 执行历史（等 backend-requests.md #1 交付后接入真实数据，见 plan.md §5） */}
        {activeTab === 'history' && (
          <div
            className="flex flex-col items-center gap-2 rounded-lg border border-border py-16 text-text-muted"
            data-testid="cron-history-coming-soon"
          >
            <p className="text-sm">{t('cron.history.comingSoon')}</p>
          </div>
        )}

        {/* 创建/编辑/模板抽屉 */}
        {drawer && (
          <CronTaskDrawer
            mode={drawer.mode}
            initial={drawer.initial}
            projects={projects}
            targetOptions={targetOptions}
            proactiveLocked={drawer.mode === 'edit' && drawer.jobId === PROACTIVE_AUTO_JOB_ID}
            onClose={() => setDrawer(null)}
            onSwitchToManual={
              drawer.mode === 'template' ? () => setDrawer({ mode: 'create', initial: drawer.initial }) : undefined
            }
            onSwitchToTemplate={
              drawer.mode === 'create'
                ? () => {
                    setDrawer(null);
                    setActiveTab('template');
                  }
                : undefined
            }
            onSubmit={(value) => {
              if (drawer.mode === 'edit') void handleEditSubmit(drawer.jobId, value);
              else void handleCreateSubmit(value);
            }}
          />
        )}

        {/* 删除确认弹窗 */}
        {confirmState?.type === 'delete' && (
          <ConfirmDialog
            title={t('cron.confirm.deleteTitle')}
            message={t('cron.confirm.deleteMessage', { name: confirmState.job.name })}
            onConfirm={() => void handleDeleteConfirm()}
            onCancel={() => setConfirmState(null)}
            loading={confirmBusy}
          />
        )}

        {/* 停止确认弹窗 */}
        {confirmState?.type === 'stop' && (
          <ConfirmDialog
            title={t('cron.confirm.stopTitle')}
            message={t('cron.confirm.stopMessage', { name: confirmState.job.name })}
            onConfirm={() => void handleStopConfirm()}
            onCancel={() => setConfirmState(null)}
            loading={confirmBusy}
          />
        )}

        {/* 立即执行确认弹窗 */}
        {confirmState?.type === 'runNow' && (
          <ConfirmDialog
            title={t('cron.confirm.runNowTitle')}
            message={t('cron.confirm.runNowMessage', { name: confirmState.job.name })}
            onConfirm={() => void handleRunNowConfirm()}
            onCancel={() => setConfirmState(null)}
            loading={confirmBusy}
          />
        )}
      </div>
    </div>
  );
}
