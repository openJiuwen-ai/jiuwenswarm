import { ChevronDown } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useWorkspaceStore } from '../../../stores';
import { rememberDesignerGraphId } from '../designerGraphLoad';
import { useDesignerStore } from '../designerStore';
import type { DesignerGraphSummary } from '../executionGraphTypes';

function graphLabel(item: DesignerGraphSummary, hasVideoLabel: string): string {
  const title = item.title?.trim() || item.graph_id;
  return item.has_video ? `${title} · ${hasVideoLabel}` : title;
}

export function DesignerRecentGraphs({
  graphId,
  currentTitle,
}: {
  graphId: string | null;
  currentTitle: string;
}) {
  const { t } = useTranslation();
  const designerGraphs = useWorkspaceStore((state) => state.designerGraphs);
  const loadDesignerGraphs = useWorkspaceStore((state) => state.loadDesignerGraphs);
  const loadGraph = useDesignerStore((state) => state.loadGraph);
  const loadStatus = useDesignerStore((state) => state.loadStatus);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const options = useMemo(
    () => designerGraphs.filter((item) => String(item.graph_id || '').trim()),
    [designerGraphs],
  );

  useEffect(() => {
    void loadDesignerGraphs();
  }, [loadDesignerGraphs]);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (options.length === 0) return null;

  const selected = options.find((item) => item.graph_id === graphId);
  const buttonLabel =
    selected ? graphLabel(selected, t('designer.hasVideo')) : currentTitle.trim() || t('designer.recentGraphs');
  const busy = loadStatus === 'loading';

  return (
    <div className="designer-page__recent" ref={rootRef}>
      <span>{t('designer.recentGraphs')}</span>
      <button
        type="button"
        className="designer-page__recent-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={busy}
        data-testid="designer-recent-graphs"
        onClick={() => {
          const next = !open;
          setOpen(next);
          if (next) void loadDesignerGraphs();
        }}
      >
        <span className="designer-page__recent-trigger-label">{buttonLabel}</span>
        <ChevronDown size={14} aria-hidden />
      </button>
      {open ? (
        <ul className="designer-page__recent-menu" role="listbox" data-testid="designer-recent-graphs-menu">
          {options.map((item) => {
            const active = item.graph_id === graphId;
            return (
              <li key={item.graph_id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={active}
                  className={`designer-page__recent-option${active ? ' is-active' : ''}`}
                  onClick={() => {
                    setOpen(false);
                    if (!item.graph_id || item.graph_id === graphId) return;
                    rememberDesignerGraphId(item.graph_id);
                    void loadGraph(item.graph_id);
                  }}
                >
                  {graphLabel(item, t('designer.hasVideo'))}
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
