/** Controls on the trajectory tab of the analysis page. */

import { memo, useEffect, useRef, useState } from 'react';
import { ChevronDown, Download, FileUp } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { TrajectoryArchiveActions, TrajectoryLaneScale } from './TrajectoryPanel';
import { IconQuestionOutline14, IconSearchOutline16 } from './primitives/icons/index.tsx';
import css from './TrajectoryAnalysisToolbar.module.css';

export interface TrajectoryAnalysisToolbarProps {
  scale: TrajectoryLaneScale;
  /** Diagnosis selection forces the shared-clock scale, so the menu stays put. */
  scaleLocked: boolean;
  onScaleChange: (scale: TrajectoryLaneScale) => void;
  turnsCollapsed: boolean;
  onToggleTurns: () => void;
  callsCollapsed: boolean;
  onToggleCalls: () => void;
  searchQuery: string;
  onSearchQueryChange: (query: string) => void;
  /** Import, export, and the attribution toggle moved from the conversation header. */
  archiveActions: TrajectoryArchiveActions | null;
}

const SCALES: readonly TrajectoryLaneScale[] = ['time', 'equal', 'tokens', 'duration'];

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
  archiveActions,
}: TrajectoryAnalysisToolbarProps) {
  const { t } = useTranslation();
  const [scaleOpen, setScaleOpen] = useState(false);
  const [foldsOpen, setFoldsOpen] = useState(false);
  const scaleRef = useRef<HTMLDivElement>(null);
  const foldsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (scaleLocked) setScaleOpen(false);
  }, [scaleLocked]);

  useEffect(() => {
    if (!scaleOpen && !foldsOpen) return undefined;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (scaleOpen && scaleRef.current?.contains(target) !== true) setScaleOpen(false);
      if (foldsOpen && foldsRef.current?.contains(target) !== true) setFoldsOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      setScaleOpen(false);
      setFoldsOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown, true);
    };
  }, [foldsOpen, scaleOpen]);

  return (
    <div className={css.root} data-testid="trajectory-analysis-toolbar">
      <div className={css.menu} ref={scaleRef}>
        <button
          type="button"
          className={css.menuButton}
          aria-label={t('trajectory.analysis.toolbar.scale')}
          aria-haspopup="listbox"
          aria-expanded={scaleOpen}
          disabled={scaleLocked}
          onClick={() => {
            setFoldsOpen(false);
            setScaleOpen(open => !open);
          }}
          data-testid="trajectory-analysis-scale"
        >
          <span>{t(`trajectory.analysis.toolbar.${scale}`)}</span>
          <ChevronDown className={css.chevron} size={14} strokeWidth={1.75} aria-hidden />
        </button>
        {scaleOpen ? (
          <div
            className={css.panel}
            role="listbox"
            aria-label={t('trajectory.analysis.toolbar.scale')}
            data-testid="trajectory-analysis-scale-menu"
          >
            {SCALES.map(item => (
              <button
                key={item}
                type="button"
                role="option"
                aria-selected={item === scale}
                onClick={() => {
                  onScaleChange(item);
                  setScaleOpen(false);
                }}
              >
                {t(`trajectory.analysis.toolbar.${item}`)}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      <div className={css.folds} ref={foldsRef}>
        <button
          type="button"
          className={css.menuButton}
          aria-haspopup="menu"
          aria-expanded={foldsOpen}
          onClick={() => {
            setScaleOpen(false);
            setFoldsOpen(open => !open);
          }}
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
        disabled={archiveActions === null || archiveActions.importing}
        aria-label={archiveActions?.importLabel ?? t('trajectory.analysis.toolbar.import')}
        title={archiveActions?.importLabel ?? t('trajectory.analysis.toolbar.import')}
        onClick={() => archiveActions?.requestImport()}
        data-testid="trajectory-archive-import"
      >
        <FileUp size={16} strokeWidth={1.75} aria-hidden />
      </button>
      <button
        type="button"
        className={css.icon}
        disabled={archiveActions === null || archiveActions.exportDisabled}
        aria-label={archiveActions?.exportLabel ?? t('trajectory.analysis.toolbar.export')}
        title={archiveActions?.exportLabel ?? t('trajectory.analysis.toolbar.export')}
        onClick={() => archiveActions?.requestExport()}
        data-testid="trajectory-archive-export"
      >
        <Download size={16} strokeWidth={1.75} aria-hidden />
      </button>
      {archiveActions?.replayActive === true ? (
        <button
          type="button"
          className={css.menuButton}
          onClick={() => archiveActions.exitReplay()}
          data-testid="trajectory-archive-exit"
        >
          {archiveActions.exitReplayLabel}
        </button>
      ) : null}
      <button
        type="button"
        className={`${css.icon} ${archiveActions?.attributionOpen === true ? css.iconActive : ''}`}
        aria-expanded={archiveActions?.attributionOpen === true}
        aria-label={archiveActions?.attributionLabel ?? t('trajectory.analysis.toolbar.attribution')}
        title={archiveActions?.attributionLabel ?? t('trajectory.analysis.toolbar.attribution')}
        disabled={archiveActions === null}
        onClick={() => archiveActions?.toggleAttribution()}
        data-testid="trajectory-attribution-toggle"
      >
        <IconQuestionOutline14 />
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
