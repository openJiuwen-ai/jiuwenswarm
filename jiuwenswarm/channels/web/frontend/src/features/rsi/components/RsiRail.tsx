/**
 * RSI 左侧实验管理栏：标题 + 创建按钮 + 实验列表。
 */
import { useTranslation } from 'react-i18next';
import type { RsiTaskListItem } from '../types';
import { statusBadgeInfo, typeDisplayLabel } from '../rsiPresentation';

interface RsiRailProps {
  tasks: RsiTaskListItem[];
  loading: boolean;
  error: string | null;
  selectedTaskId: string | null;
  onSelect: (taskId: string) => void;
  onCreate: () => void;
  onRetry: () => void;
  /** 容器宽度不足时收起（宽度驱动，动画对齐 conversation-sidebar） */
  collapsed?: boolean;
  /** 窄屏（≤1250px）悬浮模式：展开时覆盖详情区并带蒙层 */
  floating?: boolean;
  /** 收起实验栏：蒙层点击或悬浮态选中任务后调用 */
  onCollapse?: () => void;
  /** 收起态 hover 图标触发的临时展开（peek）：以悬浮层显示但不带蒙层，指针移开即收起 */
  peek?: boolean;
  /** peek 保活/收起用：面板根的鼠标进出 */
  onMouseEnter?: () => void;
  onMouseLeave?: () => void;
}

export function RsiRail({
  tasks,
  loading,
  error,
  selectedTaskId,
  onSelect,
  onCreate,
  onRetry,
  collapsed = false,
  floating = false,
  onCollapse,
  peek = false,
  onMouseEnter,
  onMouseLeave,
}: RsiRailProps) {
  const { t } = useTranslation();
  // 悬浮模式且展开时展示蒙层（对齐 conversation-sidebar__overlay），点击蒙层收起；
  // peek（hover 预览）不带蒙层：蒙层属于“固定展开”态，peek 的消失由指针移开驱动
  const showOverlay = floating && !collapsed && !peek;

  return (
    <>
      {showOverlay && (
        <div className="rsi-rail__overlay" onClick={onCollapse} role="presentation" data-testid="rsi-rail-overlay" />
      )}
      <aside
        className={`rsi-rail${floating ? ' is-floating' : ''}${collapsed ? ' is-collapsed' : ''}`}
        onMouseEnter={onMouseEnter}
        onMouseLeave={onMouseLeave}
        data-testid="rsi-rail"
      >
        <div className="rsi-rail__header">
          <span className="rsi-rail__title">{t('rsi.title')}</span>
        </div>
        <div className="rsi-rail__create">
          <button type="button" className="rsi-rail__create-btn" onClick={onCreate} data-testid="rsi-create-button">
            <svg
              viewBox="0 0 20 20"
              width="14"
              height="14"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M10 4v12M4 10h12" />
            </svg>
            {t('rsi.createExperiment')}
          </button>
        </div>

        <div className="rsi-rail__body">
          <div className="rsi-rail__body-header">RSI自进化实验</div>
          {loading && <div className="rsi-loading">{t('rsi.list.loading')}</div>}
          {!loading && error && (
            <div className="rsi-error" style={{ flexDirection: 'column', gap: 8 }}>
              <span>{t('rsi.list.error')}</span>
              <button type="button" className="rsi-btn rsi-btn--ghost" onClick={onRetry}>
                {t('common.retry', { defaultValue: '重试' })}
              </button>
            </div>
          )}
          {!loading &&
            !error &&
            tasks.map((task) => {
              const active = task.task_id === selectedTaskId;
              return (
                <div
                  key={task.task_id}
                  className={`rsi-rail__item${active ? ' rsi-rail__item--active' : ''}`}
                  onClick={() => {
                    onSelect(task.task_id);
                    // 悬浮态选中任务后收起实验栏（蒙层模式），详情拿回全宽
                    if (floating) onCollapse?.();
                  }}
                  role="button"
                  tabIndex={0}
                  data-testid="rsi-rail-item"
                  title={`${typeDisplayLabel(task.scenario, task.artifact_type)} · ${t(`rsi.detail.${statusBadgeInfo(task.status).labelKey}`)}`}
                >
                  <span className="rsi-rail__item-name">{task.name}</span>
                  {task.running && <span className="rsi-rail__running-dot" aria-label="running" />}
                </div>
              );
            })}
        </div>
      </aside>
    </>
  );
}
