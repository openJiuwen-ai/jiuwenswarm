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
}

export function RsiRail({ tasks, loading, error, selectedTaskId, onSelect, onCreate, onRetry }: RsiRailProps) {
  const { t } = useTranslation();

  return (
    <aside className="rsi-rail" data-testid="rsi-rail">
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
                onClick={() => onSelect(task.task_id)}
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
  );
}
