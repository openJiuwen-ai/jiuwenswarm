import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Pencil } from 'lucide-react';
import ScheduleEditor from './ScheduleEditor';
import ModelPicker from './ModelPicker';
import ModeSelector from './ModeSelector';
import DatePicker from './DatePicker';
import SimpleSelect from './SimpleSelect';
import TemplateClusterIcon from './TemplateClusterIcon';
import { validateCronExpr } from './cronExprValidation';
import { normalizeWakeOffsetSeconds } from './cronWakeOffset';
import { cronExprToSchedule, isOnceScheduleExpired } from './scheduleConvert';
import { TIMEZONE_OPTIONS } from './constants';
import { isDefaultLikeProject } from './cronProjectDisplay';
import type { CronTaskUI, CronTemplateUI } from '../../types/cron';
import type { ProjectInfo, WorkMode } from '../../features/workspace/projectTypes';
import { getProjectDisplayName } from '../../stores/workspaceStore';
import type { AgentMode } from '../../types';
// 会话输入框工具栏那一套 .chat-mode-select pill 下拉组件（模式/模型选择器）的 CSS，
// 抽屉里的模式/模型选择器直接复用同一套 class，跟会话界面视觉/交互完全一致。
// 这份 CSS 是普通全局样式（非 CSS Module），AgentPanel/FileViewer.tsx 已有同样 import 先例。
import '../ChatPanel/ChatPanel.css';

export { isDefaultLikeProject } from './cronProjectDisplay';

// "生效周期"依赖后端 effective_from/effective_until（见 backend-requests.md 需求3），目前后端还
// 没有这个概念，选了也不下发。之前的方案是保留字段但标注"即将上线"，用户后来觉得不如先整个隐藏，
// 等后端接口交付后再打开——代码/state（`CronTaskFormValue.effectiveDate`）都保留，不用重写
const CRON_EFFECTIVE_DATE_UI_ENABLED = false;

// 名称/描述最大长度：需与后端 jiuwenswarm/gateway/cron/models.py 里的
// CRON_JOB_NAME_MAX_LENGTH / CRON_JOB_DESCRIPTION_MAX_LENGTH 保持一致，
// 前端负责提前拦截+提示，后端负责兜底校验（防止绕过前端直接调 API/MCP 工具）。
const CRON_NAME_MAX_LENGTH = 64;
const CRON_DESCRIPTION_MAX_LENGTH = 500;

export interface CronTaskFormValue {
  name: string;
  projectDir: string | null; // 仅创建/模板创建模式使用；编辑模式不展示项目字段，不参与提交
  // 与 projectDir 配套的 project_id，下拉框选中时一并反查写入（见 projectOptions/onChange）。
  // 后端 controller.py resolve_cron_project_binding 优先信任显式 project_id，其次才按
  // project_dir 反查可见项目——只传 project_dir 会多走一层查找，带上 projectId 更直接、更可靠
  // （与会话内 cron_create_job 工具调用那条链路天然自带 project_id 的行为对齐）。
  projectId: string | null;
  // 与 projectId 配套的 work_mode（AgentOS 多用户下 Gateway 不再本地反查项目表，
  // work_mode 需由前端随 project_id 一并下发；见 index.tsx handleCreateSubmit）。
  workMode: WorkMode | null;
  modelName: string | null;
  description: string;
  /** 执行模式：单Agent('agent')/集群('team')，下发到后端 CronJob.mode（见 index.tsx handleCreateSubmit/handleEditSubmit） */
  mode: AgentMode;
  targets: string; // 推送频道，对应后端 CronJob.targets
  cronExpr: string;
  /** 提前唤醒秒数，对应后端 CronJob.wake_offset_seconds；0 表示到点执行 */
  wakeOffsetSeconds: number;
  timezone: string;
  effectiveDate: string | null; // 【backend-requests.md #3】仅前端展示，不下发
  enabled: boolean;
}

function emptyForm(): CronTaskFormValue {
  return {
    name: '',
    projectDir: null,
    projectId: null,
    workMode: null,
    modelName: null,
    description: '',
    mode: 'agent',
    targets: 'web',
    cronExpr: '',
    wakeOffsetSeconds: 0,
    timezone: 'Asia/Shanghai',
    effectiveDate: null,
    enabled: true,
  };
}

export function jobToForm(job: CronTaskUI): CronTaskFormValue {
  return {
    name: job.name,
    projectDir: null,
    projectId: null,
    workMode: null,
    modelName: job.modelName,
    description: job.description,
    mode: job.mode,
    targets: job.deliveryChannel,
    cronExpr: job.cronExpr,
    wakeOffsetSeconds: normalizeWakeOffsetSeconds(job.wakeOffsetSeconds),
    timezone: job.timezone,
    effectiveDate: null,
    enabled: job.enabled,
  };
}

export function templateToForm(tpl: CronTemplateUI, title: string, description: string): CronTaskFormValue {
  return {
    name: title,
    projectDir: null,
    projectId: null,
    workMode: null,
    modelName: null,
    description,
    mode: 'agent',
    targets: 'web',
    cronExpr: tpl.cronExpr,
    wakeOffsetSeconds: 0,
    timezone: 'Asia/Shanghai',
    effectiveDate: null,
    enabled: true,
  };
}

interface CronTaskDrawerProps {
  mode: 'create' | 'edit' | 'template';
  initial?: CronTaskFormValue;
  projects: ProjectInfo[];
  targetOptions: { value: string; label: string; disabled?: boolean }[];
  // 主动推荐自动维护的 job（proactive-tick-auto）编辑时锁定：只能改执行计划(cron表达式)和时区，
  // 其余字段（名称/模型/描述/推送频道/启用）由 Settings/cron_sync 管理，只读展示
  // （沿用 upstream 提交 59cf6de7 的约束，见 index.tsx handleEditSubmit）
  proactiveLocked?: boolean;
  onClose: () => void;
  onSubmit: (value: CronTaskFormValue) => void;
  onSwitchToManual?: () => void;
  onSwitchToTemplate?: () => void;
}

const fieldClass = 'w-full rounded-md border border-border bg-card px-3 py-1.5 text-sm text-text outline-none focus:border-accent disabled:cursor-not-allowed disabled:opacity-50';

// 后端存在两条"默认项目"记录（project_id 分别为 'default'/'default_code'，对应普通/代码工作模式），
// 二者 project_dir 均为空串，与"未选项目"状态的 value（也是空串）完全相同，SimpleSelect 无法区分
// "选中了默认项目"和"没选任何项目"，总会显示"默认项目"，与任务列表里未选项目显示"-"不一致（bug009）。
// 这里索性把所有默认类项目都从下拉框选项里过滤掉——下拉框只保留 project_dir 为非空绝对路径的真实项目，
// 不选时 SimpleSelect 找不到匹配项，走 placeholder 显示"-"，与列表里的"未选项目"语义保持一致。
function filterNonDefaultProjects(projects: ProjectInfo[]): ProjectInfo[] {
  return projects.filter((p) => !isDefaultLikeProject(p));
}

export default function CronTaskDrawer({ mode, initial, projects, targetOptions, proactiveLocked = false, onClose, onSubmit, onSwitchToManual, onSwitchToTemplate }: CronTaskDrawerProps) {
  const { t } = useTranslation();
  const [form, setForm] = useState<CronTaskFormValue>(initial ?? emptyForm());

  const handleModeChange = (nextMode: AgentMode) => {
    setForm((current) => ({ ...current, mode: nextMode }));
  };

  const submittedForm = form;

  const title = mode === 'edit' ? t('cron.drawer.titleEdit') : mode === 'template' ? t('cron.drawer.titleTemplate') : t('cron.drawer.titleCreate');
  // 显式加一条 value 为空串的"-"选项，代表"未选项目"，放在真实项目列表最后面（列表顺序：
  // 真实项目在前，"-"清空项在最后）。SimpleSelect 按 value 严格匹配，真实项目的 project_dir
  // 都是非空绝对路径，不会跟这条空串选项冲突；选中它后 onChange('') -> setForm({ ...form,
  // projectDir: '' || null }) 归一成 null，与"未选"语义一致。这样用户选了真实项目后，
  // 还能通过下拉框自己改回"未选"状态，不用关闭重开抽屉。
  const projectOptions = [
    ...filterNonDefaultProjects(projects).map((p) => ({ value: p.project_dir, label: getProjectDisplayName(p) })),
    { value: '', label: t('cron.drawer.placeholderProject') ?? '-' },
  ];
  const timezoneOptions = TIMEZONE_OPTIONS.map((tz) => ({ value: tz, label: tz }));
  // 必填项缺失时，收集清单用来在"确定"按钮旁给出具体提示（而不是只让按钮变灰、不说原因）
  const missingFieldLabels: string[] = [];
  if (!form.name.trim()) missingFieldLabels.push(t('cron.drawer.fieldName'));
  if (!form.description.trim()) missingFieldLabels.push(t('cron.drawer.fieldDescription'));
  if (!form.cronExpr.trim() || !validateCronExpr(form.cronExpr).valid) missingFieldLabels.push(t('cron.schedule.title'));
  // "单次"排班选的日期时间已经过去：字段本身不是"没填"，属于另一类校验失败，
  // 单独提示（而不是塞进"还需要填写"的缺失字段列表里，语义对不上）
  const parsedSchedule = form.cronExpr.trim() ? cronExprToSchedule(form.cronExpr) : null;
  const scheduleAlreadyExpired = parsedSchedule ? isOnceScheduleExpired(parsedSchedule, form.timezone) : false;
  const canSubmit = missingFieldLabels.length === 0 && !scheduleAlreadyExpired;
  const missingFieldsHint = missingFieldLabels.length > 0
    ? t('cron.drawer.missingFieldsHint', { fields: missingFieldLabels.join(t('cron.schedule.listSeparator')) })
    : scheduleAlreadyExpired
      ? t('cron.drawer.scheduleAlreadyExpiredHint')
      : undefined;
  const lockedTitle = proactiveLocked ? t('cron.autoManagedToggleDisabled') ?? undefined : undefined;

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-overlay-cron-drawer" data-testid="cron-drawer-overlay" onClick={onClose}>
      <div
        className="relative flex h-full w-[560px] flex-col overflow-y-auto bg-card p-6 shadow-xl animate-slide-in-right"
        data-testid="cron-drawer"
        data-variant={mode}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-6 flex items-center justify-between">
          <h3 className="text-2xl font-bold text-text-strong" data-testid="cron-drawer-title" data-variant={mode}>{title}</h3>
          <div className="flex items-center gap-3">
            {mode === 'template' && onSwitchToManual && (
              <button
                type="button"
                onClick={onSwitchToManual}
                data-testid="cron-drawer-switch-to-manual-btn"
                className="flex items-center gap-1 text-sm text-text hover:opacity-70"
              >
                <Pencil size={14} /> {t('cron.drawer.switchToManual')}
              </button>
            )}
            {mode === 'create' && onSwitchToTemplate && (
              <button
                type="button"
                onClick={onSwitchToTemplate}
                data-testid="cron-drawer-switch-to-template-btn"
                className="flex items-center gap-1 text-sm text-text hover:opacity-70"
              >
                <TemplateClusterIcon size={14} /> {t('cron.drawer.switchToTemplate')}
              </button>
            )}
            <button onClick={onClose} data-testid="cron-drawer-close-btn" className="text-text-muted hover:text-text">
              <X size={18} />
            </button>
          </div>
        </div>

        <div className="flex flex-col gap-5">
          {proactiveLocked && (
            <p className="rounded-md bg-bg-muted px-3 py-2 text-xs text-text-muted" data-testid="cron-drawer-locked-hint">{t('cron.autoManagedHint')}</p>
          )}

          <div>
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <label className="block text-sm font-bold text-text-strong" data-testid="cron-drawer-name-label">
                {t('cron.drawer.fieldName')} <span className="text-danger">*</span>
              </label>
              <span data-testid="cron-drawer-name-counter" className={`shrink-0 text-xs ${form.name.length >= CRON_NAME_MAX_LENGTH ? 'text-danger' : 'text-text-muted'}`}>
                {t('cron.drawer.charCount', { count: form.name.length, max: CRON_NAME_MAX_LENGTH })}
              </span>
            </div>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder={t('cron.drawer.placeholderInput') ?? undefined}
              maxLength={CRON_NAME_MAX_LENGTH}
              disabled={proactiveLocked}
              title={lockedTitle}
              data-testid="cron-drawer-name-input"
              className={fieldClass}
            />
            {form.name.length >= CRON_NAME_MAX_LENGTH && (
              <p className="mt-1 text-xs text-danger">{t('cron.drawer.maxLengthReachedHint', { max: CRON_NAME_MAX_LENGTH })}</p>
            )}
          </div>

          {mode !== 'edit' && (
            <div data-testid="cron-simple-select-2">
              <label className="mb-1.5 block text-sm font-bold text-text-strong" data-testid="cron-drawer-project-label">{t('cron.drawer.fieldProject')}</label>
              <SimpleSelect
                value={form.projectDir ?? ''}
                onChange={(v) => {
                  // SimpleSelect 只回传 value（即 project_dir），这里按 project_dir 反查出
                  // 对应的 project_id + work_mode 一并写入表单，提交时一起下发（见 CronTaskFormValue.projectId/workMode 注释）
                  const matched = v ? filterNonDefaultProjects(projects).find((p) => p.project_dir === v) : undefined;
                  setForm({ ...form, projectDir: v || null, projectId: matched?.project_id ?? null, workMode: matched?.work_mode ?? null });
                }}
                options={projectOptions}
                placeholder={t('cron.drawer.placeholderProject') ?? undefined}
              />
            </div>
          )}

          <div>
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <label className="block text-sm font-bold text-text-strong" data-testid="cron-drawer-description-label">
                {t('cron.drawer.fieldDescription')} <span className="text-danger">*</span>
              </label>
              <span data-testid="cron-drawer-description-counter" className={`shrink-0 text-xs ${form.description.length >= CRON_DESCRIPTION_MAX_LENGTH ? 'text-danger' : 'text-text-muted'}`}>
                {t('cron.drawer.charCount', { count: form.description.length, max: CRON_DESCRIPTION_MAX_LENGTH })}
              </span>
            </div>
            {/* 描述框 + 底部内嵌工具栏（模式/模型 pill）做成一体式容器，参考会话界面
                输入框那种"输入区 + 底部工具栏"结构——pill 直接贴在 textarea 下沿，
                不再是独立一块浮在描述框下方、中间留一大段空隙。容器用 fieldClass 的
                边框/背景，textarea 去掉自己的边框只保留内边距，视觉上连成一片。 */}
            {/* fieldClass 自带的 focus:border-accent 对这个容器 div 本身不会触发（div 不可聚焦）；
                额外加 focus-within:border-accent，textarea 获得焦点时通过子元素状态高亮容器边框，
                避免用户聚焦描述框时完全没有视觉反馈（见 review G3）。 */}
            <div className={`${fieldClass} focus-within:border-accent flex flex-col gap-2 p-0`} data-testid="cron-drawer-description-field">
              <textarea
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder={t('cron.drawer.placeholderInput') ?? undefined}
                rows={4}
                maxLength={CRON_DESCRIPTION_MAX_LENGTH}
                disabled={proactiveLocked}
                title={lockedTitle}
                data-testid="cron-drawer-description-input"
                className="w-full resize-none border-0 bg-transparent px-3 py-1.5 text-sm text-text outline-none placeholder:text-text-muted disabled:cursor-not-allowed disabled:opacity-50"
              />
              <div className="cron-drawer-mode-model-row flex items-center gap-1.5 border-t border-border/60 px-1 py-1" data-testid="cron-drawer-mode-model-row">
                <ModeSelector
                  value={form.mode}
                  onChange={handleModeChange}
                  disabled={proactiveLocked}
                />
                <ModelPicker
                  value={form.modelName}
                  onChange={(modelName) => setForm({ ...form, modelName })}
                  disabled={proactiveLocked}
                />
              </div>
            </div>
            {form.description.length >= CRON_DESCRIPTION_MAX_LENGTH && (
              <p className="mt-1 text-xs text-danger">{t('cron.drawer.maxLengthReachedHint', { max: CRON_DESCRIPTION_MAX_LENGTH })}</p>
            )}
          </div>

          <div data-testid="cron-simple-select-3">
            <label className="mb-1.5 block text-sm font-bold text-text-strong" data-testid="cron-drawer-channel-label">{t('cron.drawer.fieldChannel')}</label>
            <SimpleSelect
              value={form.targets}
              onChange={(v) => setForm({ ...form, targets: v })}
              options={targetOptions}
              disabled={proactiveLocked}
            />
          </div>

          <ScheduleEditor
            value={form.cronExpr}
            onChange={(cronExpr) => setForm({ ...form, cronExpr })}
            timezone={form.timezone}
            wakeOffsetSeconds={form.wakeOffsetSeconds}
            onWakeOffsetSecondsChange={(wakeOffsetSeconds) => setForm({ ...form, wakeOffsetSeconds })}
            wakeOffsetDisabled={proactiveLocked}
          />

          <div data-testid="cron-simple-select-4">
            <label className="mb-1.5 block text-sm font-bold text-text-strong" data-testid="cron-drawer-timezone-label">{t('cron.drawer.fieldTimezone')}</label>
            <SimpleSelect
              value={form.timezone}
              onChange={(v) => setForm({ ...form, timezone: v })}
              options={timezoneOptions}
            />
          </div>

          {CRON_EFFECTIVE_DATE_UI_ENABLED && (
            <div data-testid="cron-date-picker-1">
              <label className="mb-1.5 block text-sm font-bold text-text-strong">{t('cron.drawer.fieldEffectiveDate')}</label>
              <DatePicker
                value={form.effectiveDate ?? ''}
                onChange={(v) => setForm({ ...form, effectiveDate: v || null })}
                placeholder={t('cron.drawer.placeholderEffectiveDate') ?? undefined}
              />
              <p className="mt-1 text-xs text-text-muted">{t('cron.drawer.effectiveDateComingSoon')}</p>
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              type="button"
              disabled={proactiveLocked}
              title={lockedTitle}
              onClick={() => setForm({ ...form, enabled: !form.enabled })}
              data-testid="cron-drawer-enabled-toggle"
              data-variant={form.enabled ? 'enabled' : 'disabled'}
              className={`inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${form.enabled ? 'bg-accent' : 'bg-border-strong'}`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-card transition-transform ${form.enabled ? 'translate-x-6' : 'translate-x-1'}`}
              />
            </button>
            <span className="text-sm font-bold text-text" data-testid="cron-drawer-enabled-label" data-variant={form.enabled ? 'enabled' : 'disabled'}>
              {form.enabled ? t('cron.status.enabled') : t('cron.status.disabled')}
            </span>
          </div>
        </div>

        <div className="mt-8 flex flex-col items-center gap-2">
          <div className="flex justify-center gap-3">
            {/* title 挂在按钮外层这个非 disabled 的 span 上，而不是挂在 disabled 的 <button> 本身——
                主流浏览器（Chromium/Firefox）对 disabled 的原生表单控件不派发 hover 事件，title
                tooltip 因此根本不会弹出，之前的实现导致"按钮置灰但怎么悬停都没有提示"
                （见 2026-07-23 bugfix，bug002）。这里额外在按钮下方常驻展示同一段文案作为主要
                提示渠道，span 上的 title 只是锦上添花的 hover 备份。 */}
            <span title={missingFieldsHint}>
              <button
                onClick={() => onSubmit(submittedForm)}
                disabled={!canSubmit}
                data-testid="cron-drawer-submit-btn"
                className="rounded-full bg-cron-action px-10 py-1.5 text-sm font-bold text-cron-action-foreground hover:bg-cron-action-hover disabled:opacity-50"
              >
                {t('cron.actions.confirm')}
              </button>
            </span>
            <button
              onClick={onClose}
              data-testid="cron-drawer-cancel-btn"
              className="rounded-full border border-border bg-card px-10 py-1.5 text-sm font-bold text-text hover:bg-bg-hover"
            >
              {t('common.cancel')}
            </button>
          </div>
          {missingFieldsHint && (
            <p className="text-xs text-danger" data-testid="cron-drawer-missing-hint">{missingFieldsHint}</p>
          )}
        </div>
      </div>
    </div>
  );
}
