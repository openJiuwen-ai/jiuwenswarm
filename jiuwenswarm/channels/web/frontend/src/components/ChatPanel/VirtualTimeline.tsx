import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { useVirtualizer, type VirtualItem } from '@tanstack/react-virtual';
import type { TimelineDisplayItem } from '../../features/chatTimeline/projectTimelineItems';

const ROW_GAP = 8;
const ESTIMATED_ROW_HEIGHT = 120;

export interface TimelineViewportState {
  measurements: VirtualItem[];
  offset: number;
}

interface VirtualTimelineProps {
  items: TimelineDisplayItem[];
  renderItem: (item: TimelineDisplayItem) => ReactNode;
  initialState?: TimelineViewportState;
  onSaveState: (state: TimelineViewportState) => void;
  canLoadOlderHistory: boolean;
  historyRequestKey: string;
  onLoadOlderHistory?: () => void | Promise<void>;
}

export function VirtualTimeline({
  items,
  renderItem,
  initialState,
  onSaveState,
  canLoadOlderHistory,
  historyRequestKey,
  onLoadOlderHistory,
}: VirtualTimelineProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [scrollMargin, setScrollMargin] = useState(0);
  const getScrollElement = useCallback(
    () => containerRef.current?.closest<HTMLElement>('[data-timeline-scroll-root]') ?? null,
    [],
  );
  const getItemKey = useCallback((index: number) => items[index].key, [items]);
  const virtualizer = useVirtualizer<HTMLElement, HTMLDivElement>({
    count: items.length,
    getScrollElement,
    getItemKey,
    estimateSize: () => ESTIMATED_ROW_HEIGHT,
    initialMeasurementsCache: initialState?.measurements,
    initialOffset: () => initialState?.offset ?? items.length * (ESTIMATED_ROW_HEIGHT + ROW_GAP),
    initialRect: { width: 0, height: 1 },
    gap: ROW_GAP,
    overscan: 5,
    scrollMargin,
    anchorTo: 'end',
    // Keep size and positions in the same measurement frame without flushing React from row refs.
    directDomUpdates: true,
    directDomUpdatesMode: 'position',
    useFlushSync: false,
  });
  const setContainerRef = useCallback(
    (node: HTMLDivElement | null) => {
      containerRef.current = node;
      virtualizer.containerRef(node);
    },
    [virtualizer],
  );
  const rows = virtualizer.getVirtualItems();
  const saveStateRef = useRef(onSaveState);
  saveStateRef.current = onSaveState;

  useLayoutEffect(
    () => () => {
      saveStateRef.current({
        measurements: virtualizer.takeSnapshot(),
        offset: virtualizer.scrollOffset ?? 0,
      });
    },
    [virtualizer],
  );

  useLayoutEffect(() => {
    const container = containerRef.current;
    const scrollElement = getScrollElement();
    if (!container || !scrollElement) return;
    const measureMargin = () => {
      setScrollMargin(
        container.getBoundingClientRect().top -
          scrollElement.getBoundingClientRect().top +
          scrollElement.scrollTop -
          scrollElement.clientTop,
      );
    };
    measureMargin();
    const observer = new ResizeObserver(measureMargin);
    observer.observe(scrollElement);
    if (container.parentElement) observer.observe(container.parentElement);
    return () => observer.disconnect();
  }, [getScrollElement]);

  // The virtualizer owns prepend anchoring. Loading older data is independent
  // of which rows happen to be mounted, including a history shorter than a page.
  const lastAutoFillRequestRef = useRef<string | null>(null);
  useLayoutEffect(() => {
    const root = getScrollElement();
    const container = containerRef.current;
    if (!root || !container || !canLoadOlderHistory || !onLoadOlderHistory) return;
    const fillViewport = () => {
      if (
        root.clientHeight <= 0 ||
        root.scrollHeight > root.clientHeight ||
        lastAutoFillRequestRef.current === historyRequestKey
      )
        return;
      lastAutoFillRequestRef.current = historyRequestKey;
      void onLoadOlderHistory();
    };
    fillViewport();
    const observer = new ResizeObserver(fillViewport);
    observer.observe(root);
    observer.observe(container);
    return () => observer.disconnect();
  }, [canLoadOlderHistory, getScrollElement, historyRequestKey, onLoadOlderHistory]);

  return (
    <div
      ref={setContainerRef}
      className="chat-timeline"
      data-testid="chat-panel-timeline"
      data-virtualized="true"
      style={{ display: 'block' }}
    >
      {rows.map((row) => (
        <div
          key={row.key}
          className="chat-timeline-row"
          ref={virtualizer.measureElement}
          data-index={row.index}
          data-testid="chat-panel-timeline-row"
          data-variant={items[row.index].key}
          style={{
            position: 'absolute',
            left: 0,
            width: '100%',
            display: 'flex',
            flexDirection: 'column',
            gap: ROW_GAP,
          }}
        >
          {renderItem(items[row.index])}
        </div>
      ))}
    </div>
  );
}
