/**
 * Shared time axis of the team lanes: one idle-compressed wall-clock ruler
 * every lane is drawn against, plus the zoom window the lanes share.
 */

import { useCallback, useMemo, useRef, useState, type MutableRefObject } from 'react';
import type { TrajectorySnapshot } from '../trajectory/model';
import type { TrajectoryTimeRange } from '../trajectory/timeline';
import {
  clampSharedViewport,
  collectWallClockSamples,
  compressedToWall,
  compressSharedTimeline,
  wallToCompressed,
  type CompressedTimeline,
  type WallClockDomain,
  type WallClockSample,
} from './diagnosisTimeSelection';

export interface SharedTimeAxis {
  /** Ruler domain, `null` while no lane has timing. */
  domain: WallClockDomain | null;
  /** The same domain as a lane range, for the lane overview. */
  sharedTimeDomain: TrajectoryTimeRange | null;
  /** Maps a lane's wall-clock time onto the compressed axis. */
  mapSharedTime: ((time: number) => number) | undefined;
  /** Axis position back to a wall-clock instant, `undefined` while no lane has timing. */
  toWall: ((axisMs: number) => number) | undefined;
  /** Visible slice of the domain, `null` for the whole axis. */
  viewport: TrajectoryTimeRange | null;
  updateViewport: (next: TrajectoryTimeRange | null) => void;
  /** Latest requested viewport; lanes read it while a wheel gesture is in flight. */
  viewportRef: MutableRefObject<TrajectoryTimeRange | null>;
}

/**
 * Build the shared axis of a session's lanes.
 *
 * @param enabled - when false the axis stays empty and costs nothing.
 * @param sessionId - a new session drops the previous zoom window.
 * @param snapshots - per-lane projections the ruler is measured from.
 */
export function useSharedTimeAxis(
  enabled: boolean,
  sessionId: string,
  snapshots: ReadonlyMap<string, TrajectorySnapshot>,
): SharedTimeAxis {
  const [viewport, setViewport] = useState<TrajectoryTimeRange | null>(null);
  const viewportRef = useRef<TrajectoryTimeRange | null>(null);
  const renderedViewportRef = useRef<TrajectoryTimeRange | null>(null);
  const [scopeSessionId, setScopeSessionId] = useState(sessionId);
  const scopeSessionChanged = scopeSessionId !== sessionId;
  if (scopeSessionChanged) {
    setScopeSessionId(sessionId);
    setViewport(null);
    viewportRef.current = null;
    renderedViewportRef.current = null;
  }
  const activeViewport = scopeSessionChanged ? null : viewport;

  const compressed = useMemo<CompressedTimeline | null>(() => {
    if (!enabled) return null;
    const samples: WallClockSample[] = [];
    for (const snapshot of snapshots.values()) {
      samples.push(...collectWallClockSamples(snapshot.turns));
    }
    return compressSharedTimeline(samples, Date.now());
  }, [enabled, snapshots]);
  const durationMs = compressed?.durationMs ?? 0;
  const domain = useMemo<WallClockDomain | null>(
    () => (compressed === null ? null : { startMs: 0, endMs: compressed.durationMs }),
    [compressed],
  );
  const sharedTimeDomain = useMemo<TrajectoryTimeRange | null>(
    () => (domain === null ? null : { start: domain.startMs, end: domain.endMs }),
    [domain],
  );
  const mapSharedTime = useMemo(() => {
    if (compressed === null) return undefined;
    const timeline: CompressedTimeline = compressed;
    return (time: number) => wallToCompressed(timeline, time);
  }, [compressed]);
  const toWall = useMemo(() => {
    if (compressed === null) return undefined;
    const timeline: CompressedTimeline = compressed;
    return (axisMs: number) => compressedToWall(timeline, axisMs);
  }, [compressed]);

  const visibleViewport = useMemo(
    () => clampSharedViewport(activeViewport, 0, durationMs),
    [activeViewport, durationMs],
  );
  if (viewportRef.current === renderedViewportRef.current) {
    viewportRef.current = visibleViewport;
  }
  renderedViewportRef.current = visibleViewport;
  const updateViewport = useCallback((next: TrajectoryTimeRange | null) => {
    const clamped = clampSharedViewport(next, 0, durationMs);
    viewportRef.current = clamped;
    setViewport(clamped);
  }, [durationMs]);

  return {
    domain,
    sharedTimeDomain,
    mapSharedTime,
    toWall,
    viewport: visibleViewport,
    updateViewport,
    viewportRef,
  };
}
