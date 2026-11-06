import { useAdaptiveTooltip } from '../../../hooks/useAdaptiveTooltip';
import { useTranslation } from 'react-i18next';
import { formatSlashDate, type JiuwenMateYearHeatmap } from '../officeState';

interface ActivityHeatmapProps {
  heatmap: JiuwenMateYearHeatmap;
  hoveredCellId: string | null;
  onHover: (cellId: string | null) => void;
}

const WEEKDAY_ROWS: Record<number, string> = {
  1: 'jiuwenmate.office.weekdays.mon',
  3: 'jiuwenmate.office.weekdays.wed',
  5: 'jiuwenmate.office.weekdays.fri',
};

export function HeatmapLegend() {
  const { t } = useTranslation();
  return (
    <div className="jiuwenmate-heatmap__legend" data-testid="jiuwenmate-heatmap-legend">
      <span>{t('jiuwenmate.office.legendLow')}</span>
      {[0, 1, 2, 3, 4].map((level) => (
        <i key={level} data-level={level} />
      ))}
      <span>{t('jiuwenmate.office.legendHigh')}</span>
    </div>
  );
}

export function ActivityHeatmap({ heatmap, hoveredCellId, onHover }: ActivityHeatmapProps) {
  const { t } = useTranslation();
  const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top', multiline: true });
  const columns = heatmap.weeks.length;
  return (
    <div className="jiuwenmate-heatmap-frame" style={{ ['--jiuwenmate-heatmap-columns' as string]: columns }}>
      <div className="jiuwenmate-heatmap__months" aria-hidden>
        {heatmap.months.map((item) => (
          <span key={`${item.month}-${item.column}`} style={{ gridColumnStart: item.column + 1 }}>
            {t('jiuwenmate.office.monthLabel', { month: item.month })}
          </span>
        ))}
      </div>
      <div className="jiuwenmate-heatmap__body">
        <div className="jiuwenmate-heatmap__weekdays" aria-hidden>
          {Array.from({ length: 7 }, (_, row) => (
            <span key={row}>{WEEKDAY_ROWS[row] ? t(WEEKDAY_ROWS[row]) : ''}</span>
          ))}
        </div>
        <div className="jiuwenmate-heatmap" data-testid="jiuwenmate-activity-heatmap" data-variant="year">
          {heatmap.weeks.map((week) =>
            week.map((cell) => {
              if (!cell.inRange) return <span key={cell.id} className="jiuwenmate-heatmap__cell jiuwenmate-heatmap__cell--blank" />;
              const hovered = hoveredCellId === cell.id;
              return (
                <button
                  key={cell.id}
                  type="button"
                  className="jiuwenmate-heatmap__cell"
                  data-level={cell.level}
                  data-testid="jiuwenmate-heatmap-cell"
                  data-variant={cell.id}
                  aria-label={formatSlashDate(cell.date)}
                  aria-pressed={hovered}
                  data-tooltip={[
                    formatSlashDate(cell.date),
                    t('jiuwenmate.office.tooltipTasks', { count: cell.tasks }),
                    t('jiuwenmate.office.tooltipHours', { hours: cell.hours }),
                  ].join('\n')}
                  onMouseEnter={(event) => { onHover(cell.id); handlers.onMouseEnter(event); }}
                  onMouseLeave={() => { onHover(null); handlers.onMouseLeave(); }}
                  onFocus={(event) => { onHover(cell.id); handlers.onFocus(event); }}
                  onBlur={() => { onHover(null); handlers.onBlur(); }}
                />
              );
            }),
          )}
        </div>
      </div>
      {tooltip}
    </div>
  );
}
