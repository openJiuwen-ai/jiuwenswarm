/**
 * 技能总谱页签视图（索引推荐提示 + 交响编排卡片 + 图谱画布）
 *
 * 从 index.tsx 抽取，结构与样式保持不变。
 */
import type { MutableRefObject } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, Music2 } from 'lucide-react';
import { SkillGraphPanel, type SkillGraphPanelHandle } from '../SkillGraphPanel';
import { Switch } from '../Switch';

interface SkillGraphTabProps {
  isConnected: boolean;
  indexRecommendationVisible: boolean;
  indexRecommendationBuilding: boolean;
  onDismissRecommendation: () => void;
  onBuildRecommendedIndex: () => void;
  symphonySaveError: string | null;
  symphonySaving: boolean;
  symphonyEnabledDraft: boolean;
  onUpdateSymphonyEnabled: (enabled: boolean) => void;
  skillGraphPanelRef: MutableRefObject<SkillGraphPanelHandle | null>;
  onGraphReadingChange: (reading: boolean) => void;
  onStartRetrievalIndexBuild: (force: boolean, useDefaultProfile?: boolean) => Promise<boolean>;
  graphActionError: string | null;
  onExternalErrorClear: () => void;
}

export function SkillGraphTab({
  isConnected,
  indexRecommendationVisible,
  indexRecommendationBuilding,
  onDismissRecommendation,
  onBuildRecommendedIndex,
  symphonySaveError,
  symphonySaving,
  symphonyEnabledDraft,
  onUpdateSymphonyEnabled,
  skillGraphPanelRef,
  onGraphReadingChange,
  onStartRetrievalIndexBuild,
  graphActionError,
  onExternalErrorClear,
}: SkillGraphTabProps) {
  const { t } = useTranslation();
  return (
    <div data-testid="skill-panel-graph-view" className="page-shell mt-4 flex flex-1 min-h-0 flex-col gap-3">
      {indexRecommendationVisible ? (
        <div
          className="flex flex-none flex-col gap-3 rounded-lg border border-warn bg-warn-subtle px-4 py-3"
          data-testid="skill-panel-index-recommendation"
        >
          <p className="whitespace-pre-line text-sm leading-6 text-text">{t('skills.retrieval.indexRecommended')}</p>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="rounded-lg border border-border bg-panel px-4 py-2 text-sm text-text hover:bg-secondary/50 disabled:cursor-not-allowed disabled:opacity-60"
              disabled={indexRecommendationBuilding}
              onClick={onDismissRecommendation}
            >
              {t('skills.retrieval.recommendationDismiss')}
            </button>
            <button
              type="button"
              className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
              disabled={indexRecommendationBuilding}
              onClick={onBuildRecommendedIndex}
            >
              {indexRecommendationBuilding ? <Loader2 size={15} className="animate-spin" /> : null}
              {t('skills.retrieval.recommendationBuild')}
            </button>
          </div>
        </div>
      ) : null}
      <div
        data-testid="skill-panel-graph-orchestration-card"
        className="flex flex-none flex-wrap items-center justify-between gap-4 rounded-lg border border-border bg-panel p-4"
      >
        <div className="min-w-[240px] flex-1">
          <div className="flex items-start gap-2">
            <Music2 size={28} className="mt-1 flex-shrink-0 text-accent" aria-hidden="true" />
            <div className="min-w-0">
              <p data-testid="skill-panel-graph-definition" className="text-xs leading-5 text-text-muted">
                {t('skills.graph.orchestration.graphDefinition')}
              </p>
              <p
                data-testid="skill-panel-graph-orchestration-description"
                className="text-xs leading-5 text-text-muted"
              >
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
              <span className="text-xs text-text-muted">{t('skills.graph.orchestration.saving')}</span>
            </>
          ) : null}
          <Switch
            checked={symphonyEnabledDraft}
            onChange={(enabled) => onUpdateSymphonyEnabled(enabled)}
            disabled={!isConnected || symphonySaving}
            title={t(
              isConnected ? 'skills.graph.orchestration.toggleLabel' : 'skills.graph.orchestration.connectionRequired',
            )}
          />
        </div>
      </div>
      <div className="flex-1 min-h-0">
        <SkillGraphPanel
          ref={skillGraphPanelRef}
          onReadingChange={onGraphReadingChange}
          onBuildAccepted={(mode) => void onStartRetrievalIndexBuild(mode === 'full')}
          externalError={graphActionError}
          onExternalErrorClear={onExternalErrorClear}
        />
      </div>
    </div>
  );
}
