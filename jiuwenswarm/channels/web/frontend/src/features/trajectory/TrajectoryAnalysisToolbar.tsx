/** Controls on the trajectory tab of the analysis page. */

import { memo, useEffect, useRef, useState } from 'react';
import { ChevronDown, Download } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryLaneScale } from './TrajectoryPanel';
import { IconSearchOutline16 } from './primitives/icons/index.tsx';
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
  const [foldsOpen, setFoldsOpen] = useState(false);
  const foldsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!foldsOpen) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      if (foldsRef.current?.contains(event.target as Node) === true) return;
      setFoldsOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setFoldsOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown, true);
    };
  }, [foldsOpen]);

  return (
    <div className={css.root} data-testid="trajectory-analysis-toolbar">
      <label className={css.menu}>
        <select
          className={css.menuControl}
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
        <ChevronDown className={css.chevron} size={14} strokeWidth={1.75} aria-hidden />
      </label>
      <div className={css.folds} ref={foldsRef}>
        <button
          type="button"
          className={css.menuButton}
          aria-haspopup="menu"
          aria-expanded={foldsOpen}
          onClick={() => setFoldsOpen(open => !open)}
          data-testid="trajectory-analysis-folds"
        >
          <span>{t('trajectory.analysis.toolbar.folds')}</span>
          <ChevronDown className={css.chevron} size={14} strokeWidth={1.75} aria-hidden />
        </button>
        {foldsOpen ? (
          <div className={css.panel} role="menu" data-testid="trajectory-analysis-folds-menu">
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={turnsCollapsed}
              onClick={onToggleTurns}
              data-testid="trajectory-analysis-turns"
            >
              {turnsCollapsed
                ? t('trajectory.analysis.toolbar.expandTurns')
                : t('trajectory.analysis.toolbar.collapseTurns')}
            </button>
            <button
              type="button"
              role="menuitemcheckbox"
              aria-checked={callsCollapsed}
              onClick={onToggleCalls}
              data-testid="trajectory-analysis-tools"
            >
              {callsCollapsed
                ? t('trajectory.analysis.toolbar.expandTools')
                : t('trajectory.analysis.toolbar.collapseTools')}
            </button>
          </div>
        ) : null}
      </div>
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
        <span className={css.searchGlyph} aria-hidden="true">
          <IconSearchOutline16 size={14} />
        </span>
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
