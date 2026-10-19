/** Controls on the trajectory tab of the analysis page. */

import { memo } from 'react';
import { Download, FileText } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryLaneScale } from './TrajectoryPanel';
import css from './TrajectoryAnalysisToolbar.module.css';

export interface TrajectoryAnalysisToolbarProps {
  scale: TrajectoryLaneScale;
  /** Diagnosis selection forces the duration scale, so the menu stays put. */
  scaleLocked: boolean;
  onScaleChange: (scale: TrajectoryLaneScale) => void;
  turnsCollapsed: boolean;
  onToggleTurns: () => void;
  callsCollapsed: boolean;
  onToggleCalls: () => void;
  searchQuery: string;
  onSearchQueryChange: (query: string) => void;
  onDownload: () => void;
}

const SCALES: readonly TrajectoryLaneScale[] = ['duration', 'tokens', 'equal'];

export const TrajectoryAnalysisToolbar = memo(function TrajectoryAnalysisToolbar({
  scale,
  scaleLocked,
  onScaleChange,
  turnsCollapsed,
  onToggleTurns,
  callsCollapsed,
  onToggleCalls,
  searchQuery,
  onSearchQueryChange,
  onDownload,
}: TrajectoryAnalysisToolbarProps) {
  const { t } = useTranslation();
  return (
    <div className={css.root} data-testid="trajectory-analysis-toolbar">
      <select
        className={css.scale}
        aria-label={t('trajectory.analysis.toolbar.scale')}
        value={scale}
        disabled={scaleLocked}
        onChange={event => onScaleChange(event.currentTarget.value as TrajectoryLaneScale)}
        data-testid="trajectory-analysis-scale"
      >
        {SCALES.map(item => (
          <option key={item} value={item}>
            {t(`trajectory.analysis.toolbar.${item}`)}
          </option>
        ))}
      </select>
      <button
        type="button"
        className={css.toggle}
        aria-pressed={turnsCollapsed}
        title={turnsCollapsed
          ? t('trajectory.analysis.toolbar.expandTurns')
          : t('trajectory.analysis.toolbar.collapseTurns')}
        onClick={onToggleTurns}
        data-testid="trajectory-analysis-turns"
      >
        {t('trajectory.analysis.toolbar.turns')}
      </button>
      <button
        type="button"
        className={css.toggle}
        aria-pressed={callsCollapsed}
        title={callsCollapsed
          ? t('trajectory.analysis.toolbar.expandTools')
          : t('trajectory.analysis.toolbar.collapseTools')}
        onClick={onToggleCalls}
        data-testid="trajectory-analysis-tools"
      >
        {t('trajectory.analysis.toolbar.tools')}
      </button>
      <button
        type="button"
        className={css.icon}
        aria-label={t('trajectory.analysis.toolbar.text')}
        title={t('trajectory.analysis.toolbar.text')}
        onClick={() => {}}
        data-testid="trajectory-analysis-text"
      >
        <FileText size={15} strokeWidth={1.75} aria-hidden />
      </button>
      <button
        type="button"
        className={css.icon}
        aria-label={t('trajectory.analysis.toolbar.download')}
        title={t('trajectory.analysis.toolbar.download')}
        onClick={onDownload}
        data-testid="trajectory-analysis-download"
      >
        <Download size={15} strokeWidth={1.75} aria-hidden />
      </button>
      <label className={css.search}>
        <span className={css.searchGlyph} aria-hidden="true">⌕</span>
        <input
          type="search"
          className={css.searchInput}
          value={searchQuery}
          placeholder={t('trajectory.analysis.toolbar.searchPlaceholder')}
          aria-label={t('trajectory.analysis.toolbar.search')}
          onChange={event => onSearchQueryChange(event.currentTarget.value)}
          data-testid="trajectory-analysis-search"
        />
      </label>
    </div>
  );
});
