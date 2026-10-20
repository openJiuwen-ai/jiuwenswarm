/** Standalone trajectory explorer assembled from the DSH presentation components. */

import {
  memo, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode,
  type RefObject,
} from 'react'
import type {
  TrajectoryRequest, TrajectorySnapshot, TrajectoryTurnModel,
} from '../trajectory/model.ts'
import { trajectoryRecordId } from '../trajectory/record.ts'
import {
  resolveTimelineMode,
  trajectoryTimelineFocusIndexes,
  type TrajectoryTimelineMode,
  type TrajectoryTimeRange,
} from '../trajectory/timeline.ts'
import { TrajectorySearchIndex } from '../trajectory/search-index.ts'
import {
  TrajectoryThemeProvider, type TrajectoryColorMode,
} from '../theme/context.tsx'
import { TrajectoryTable } from './TrajectoryTable.tsx'
import { TrajectoryTimeline } from './TrajectoryTimeline.tsx'
import { TrajectoryToolbar } from './TrajectoryToolbar.tsx'
import {
  trajectoryTranslator, type TrajectoryKey, type TrajectoryTranslate,
} from './i18n.ts'
import css from './TrajectoryExplorer.module.css'

const EMPTY_TURN_IDS: ReadonlySet<number> = new Set()
const EMPTY_RECORD_IDS: ReadonlySet<string> = new Set()

interface ExplorerStyle extends CSSProperties {
  '--trajectory-bottom-inset'?: string
}

/** View controls shared by every member in a Team trajectory workspace. */
export interface TrajectoryViewState {
  actualDuration: boolean
  actualTime: boolean
  tokenView: boolean
  turnsCollapsed: boolean
  callsCollapsed: boolean
}

/** Ordinary React props; no Cordis, Session, slot, or locale service is required. */
export interface TrajectoryExplorerProps {
  /** Whether this mounted explorer is currently visible and may run its live clock. */
  active?: boolean
  /** Atomic read model produced by an OTel projector or a static fixture. */
  snapshot?: TrajectorySnapshot
  /** Static grouped records; ignored when `snapshot` is supplied. */
  turns?: readonly TrajectoryTurnModel[]
  /** Request-inspector metadata for static `turns`. */
  requests?: readonly TrajectoryRequest[]
  /** Whether the initial tail has not reached the browser yet. */
  loading?: boolean
  /** Retryable store or transport failure shown without hiding loaded records. */
  error?: string | null
  /** Override individual toolbar labels. */
  messages?: Partial<Record<TrajectoryKey, string>>
  /** Full custom translator; takes precedence over `messages`. */
  translate?: TrajectoryTranslate
  /** Space reserved for a host-owned bottom overlay. */
  bottomInset?: number | string
  /** Local viewer palette. The host remains responsible for choosing it. */
  colorMode?: TrajectoryColorMode
  /** Full explorer, overview-only swimlane, or only the record ledger. */
  displayMode?: 'full' | 'overview' | 'records'
  /** Hide the local toolbar when a host renders one shared toolbar. */
  showToolbar?: boolean
  /** Hide only the local view controls while preserving member search. */
  showToolbarViewControls?: boolean
  /** Hide the local search field. A host can still filter through `searchQuery`. */
  showSearch?: boolean
  /**
   * JiuwenSwarm extension: host-owned ledger query. When set, it replaces this
   * explorer's own field so several lanes can share one search.
   */
  searchQuery?: string
  /** Host-owned member control placed before the local search field. */
  toolbarAddon?: ReactNode
  /** Optional host-controlled view state shared across multiple explorers. */
  viewState?: TrajectoryViewState
  /** Expand an overview-only host after a timeline record is activated. */
  onOverviewActivate?: () => void
  /**
   * JiuwenSwarm extension: `traceId:spanId` anchors to emphasise on the timeline
   * (diagnosis evidence). The ledger is never filtered by this set.
   */
  highlightStepKeys?: ReadonlySet<string>
  /**
   * JiuwenSwarm extension: reports the wall-clock hull (Unix ms) of the records
   * covered by the current timeline drag selection, or `null` when it is cleared.
   */
  onTimelineSelectionChange?: (selection: TrajectoryWallClockSelection | null) => void
  /** JiuwenSwarm extension: bump to clear this explorer's own timeline selection. */
  clearSelectionToken?: number
  /**
   * Controlled timeline brush, in the active projection's domain. When
   * `onTimelineRangeChange` is set, this replaces the explorer's own range.
   */
  timelineRange?: TrajectoryTimeRange | null
  /** Reports a drag on this lane, including a clear. */
  onTimelineRangeChange?: (range: TrajectoryTimeRange | null) => void
  /** Shared wall-clock domain so this lane lines up with the diagnosis axis. */
  sharedTimeDomain?: TrajectoryTimeRange | null
  /** Maps this lane's span times onto `sharedTimeDomain`. */
  mapSharedTime?: (time: number) => number
  /** Left-drag selects. The wheel still zooms when a shared viewport is controlled. */
  lockTimelineViewport?: boolean
  /** With a locked viewport, clicking an Input block still selects its range. */
  spanSelectsRange?: boolean
  /**
   * A short press on the overview selects that block's ledger row. Diagnosis
   * leaves this off so the press still paints the shared brush.
   */
  clickSelectsRecord?: boolean
  /** Visible window shared with the diagnosis axis and the other lanes. */
  sharedViewport?: TrajectoryTimeRange | null
  onSharedViewportChange?: (viewport: TrajectoryTimeRange | null) => void
  /** Latest shared window for a wheel burst. */
  sharedViewportRef?: RefObject<TrajectoryTimeRange | null>
  /** How a controlled brush is drawn. Diagnosis uses a light-blue region. */
  timelineRangeAppearance?: 'focus' | 'region'
  className?: string
}

/** Wall-clock hull of a timeline selection, in Unix milliseconds. */
export interface TrajectoryWallClockSelection {
  startMs: number
  endMs: number
}

/** Extract the `traceId:spanId` anchor from a projected record id. */
function stepKeyOfRecordId(recordId: string): string | null {
  const first = recordId.indexOf(':')
  if (first <= 0) return null
  const second = recordId.indexOf(':', first + 1)
  return second < 0 ? recordId : recordId.slice(0, second)
}

/** Whether a projected cell is one of the spans a diagnosis asked to emphasise. */
export function cellMatchesHighlightKeys(
  recordId: string,
  traceDetail: unknown,
  keys: ReadonlySet<string>,
): boolean {
  const key = stepKeyOfRecordId(recordId)
  if (key !== null && keys.has(key)) return true
  if (!recordId.startsWith('v2:')) return false
  const detailKeys = stepKeysOfTraceDetail(traceDetail)
  return detailKeys !== null && [...detailKeys].some(item => keys.has(item))
}

/**
 * JiuwenSwarm extension: extract every `traceId:spanId` anchor carried by a
 * v2 event cell's raw OTLP export record (`cell.traceDetail`). v2 cell record
 * ids are `v2:<subject>:<eventId>:<suffix>` and can never match a span anchor
 * directly, so the diagnosis highlighter falls back to these anchors.
 */
function stepKeysOfTraceDetail(traceDetail: unknown): ReadonlySet<string> | null {
  if (typeof traceDetail !== 'object' || traceDetail === null) return null
  const resourceSpans = (traceDetail as { resourceSpans?: unknown }).resourceSpans
  if (!Array.isArray(resourceSpans)) return null
  const keys = new Set<string>()
  for (const resourceSpan of resourceSpans) {
    if (typeof resourceSpan !== 'object' || resourceSpan === null) continue
    const scopeSpans = (resourceSpan as { scopeSpans?: unknown }).scopeSpans
    if (!Array.isArray(scopeSpans)) continue
    for (const scopeSpan of scopeSpans) {
      if (typeof scopeSpan !== 'object' || scopeSpan === null) continue
      const spans = (scopeSpan as { spans?: unknown }).spans
      if (!Array.isArray(spans)) continue
      for (const span of spans) {
        if (typeof span !== 'object' || span === null) continue
        const { traceId, spanId } = span as { traceId?: unknown; spanId?: unknown }
        if (typeof traceId === 'string' && typeof spanId === 'string' && traceId !== '' && spanId !== '') {
          keys.add(`${traceId}:${spanId}`)
        }
      }
    }
  }
  return keys
}

function searchIndexes(
  index: TrajectorySearchIndex,
  layouts: readonly (readonly TrajectoryTurnModel[])[],
  query: string,
): ReadonlySet<number> | null {
  // Without a query nothing is filtered, so the index is left to catch up on
  // the first keystroke instead of reindexing every published window.
  if (query.trim() === '') return null
  // `layouts` is memoized on `turns`, which lets the index skip the walk when
  // only the query changed.
  index.update(layouts)
  const ids = index.search(query)
  const turns = layouts[0] ?? []
  if (ids === null) return null
  const matches = new Set<number>()
  for (const turn of turns) {
    for (const group of turn.groups) {
      for (const cell of group.cells) {
        if (ids.has(trajectoryRecordId(cell))) matches.add(cell.index)
      }
    }
  }
  return matches
}

/** Render the full explorer or a host-owned overview with only the record ledger. */
export const TrajectoryExplorer = memo(function TrajectoryExplorer({
  active = true,
  snapshot,
  turns: staticTurns = [],
  requests: staticRequests,
  loading = false,
  error = null,
  messages,
  translate,
  bottomInset = 0,
  colorMode = 'light',
  displayMode = 'full',
  showToolbar = true,
  showToolbarViewControls = true,
  showSearch = true,
  searchQuery: controlledSearchQuery,
  toolbarAddon,
  viewState,
  onOverviewActivate,
  highlightStepKeys,
  onTimelineSelectionChange,
  clearSelectionToken = 0,
  timelineRange: controlledTimelineRange,
  onTimelineRangeChange,
  sharedTimeDomain = null,
  mapSharedTime,
  lockTimelineViewport = false,
  spanSelectsRange = false,
  clickSelectsRecord = false,
  sharedViewport = null,
  onSharedViewportChange,
  sharedViewportRef,
  timelineRangeAppearance = 'focus',
  className,
}: TrajectoryExplorerProps) {
  const turns = snapshot?.turns ?? staticTurns
  const requests = snapshot?.requests ?? staticRequests
  const hasRunningCells = useMemo(() => turns.some(turn => (
    turn.groups.some(group => group.cells.some(cell => cell.status === 'running'))
  )), [turns])
  const [liveNowMilliseconds, setLiveNowMilliseconds] = useState(() => Date.now())
  useEffect(() => {
    if (!active || !hasRunningCells) return undefined
    const update = () => setLiveNowMilliseconds(Date.now())
    update()
    const interval = window.setInterval(update, 250)
    const onVisibility = () => {
      if (document.visibilityState === 'visible') update()
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [active, hasRunningCells])
  const [actualDuration, setActualDuration] = useState(false)
  const [actualTime, setActualTime] = useState(false)
  const [tokenView, setTokenView] = useState(false)
  const [collapsedTurns, setCollapsedTurns] = useState<ReadonlySet<number>>(EMPTY_TURN_IDS)
  const [collapsedAssistants, setCollapsedAssistants] =
    useState<ReadonlySet<string>>(EMPTY_RECORD_IDS)
  // When the analysis page forces every turn/tool fold via viewState, a timeline
  // click still needs to open the owning fold so the ledger can scroll to it.
  const [revealedTurns, setRevealedTurns] = useState<ReadonlySet<number>>(EMPTY_TURN_IDS)
  const [revealedAssistants, setRevealedAssistants] =
    useState<ReadonlySet<string>>(EMPTY_RECORD_IDS)
  const [localSearchQuery, setLocalSearchQuery] = useState('')
  const searchQuery = controlledSearchQuery ?? localSearchQuery
  const [searchIndex] = useState(() => new TrajectorySearchIndex())
  const searchLayouts = useMemo(() => [turns], [turns])
  const [uncontrolledTimelineRange, setUncontrolledTimelineRange] = useState<TrajectoryTimeRange | null>(null)
  const rangeControlled = onTimelineRangeChange !== undefined
  const timelineRange = rangeControlled ? (controlledTimelineRange ?? null) : uncontrolledTimelineRange
  const setTimelineRange = rangeControlled ? onTimelineRangeChange : setUncontrolledTimelineRange
  const [selectedTimelineIndex, setSelectedTimelineIndex] = useState<number | null>(null)
  const [recordSelection, setRecordSelection] = useState<{ readonly index: number } | null>(null)
  const [recordFocus, setRecordFocus] = useState<{ readonly index: number } | null>(null)
  const [scrollToEndSignal, setScrollToEndSignal] = useState(0)
  const wasActiveRef = useRef(false)
  // A click on a timeline block already names the ledger row to show. Expanding
  // the lane for that click must not then jump the ledger to its newest record.
  const preferRecordScrollRef = useRef(false)
  // Whenever the explorer becomes visible (first mount while active, a chat ->
  // trajectory tab switch, or a subject tab change), ask the ledger to jump to
  // the newest record. The ref starts false so an initially-active explorer
  // still requests the tail on first render.
  useEffect(() => {
    const ledgerActive = active && displayMode !== 'overview'
    const becameActive = ledgerActive && !wasActiveRef.current
    wasActiveRef.current = ledgerActive
    if (becameActive && !preferRecordScrollRef.current) setScrollToEndSignal(value => value + 1)
    preferRecordScrollRef.current = false
  }, [active, displayMode])
  const resolvedActualDuration = viewState?.actualDuration ?? actualDuration
  const resolvedActualTime = viewState?.actualTime ?? actualTime
  const resolvedTokenView = viewState?.tokenView ?? tokenView
  const t = useMemo(
    () => translate ?? trajectoryTranslator(messages),
    [messages, translate],
  )
  const timelineMode: TrajectoryTimelineMode = resolveTimelineMode({
    tokenView: resolvedTokenView,
    actualDuration: resolvedActualDuration,
    actualTime: resolvedActualTime,
  })
  useEffect(() => {
    if (onTimelineRangeChange !== undefined) return
    setUncontrolledTimelineRange(null)
  }, [onTimelineRangeChange, timelineMode])
  const searchMatchIndexes = useMemo(
    () => searchIndexes(searchIndex, searchLayouts, searchQuery),
    [searchIndex, searchLayouts, searchQuery],
  )
  const timelineFocusIndexes = useMemo(
    () => timelineRange === null
      ? null
      : trajectoryTimelineFocusIndexes(
        turns,
        timelineRange,
        timelineMode,
        liveNowMilliseconds,
      ),
    [liveNowMilliseconds, timelineMode, timelineRange, turns],
  )
  const highlightIndexes = useMemo<ReadonlySet<number> | null>(() => {
    if (highlightStepKeys === undefined || highlightStepKeys.size === 0) return null
    const matches = new Set<number>()
    for (const turn of turns) {
      for (const group of turn.groups) {
        for (const cell of group.cells) {
          if (cellMatchesHighlightKeys(trajectoryRecordId(cell), cell.traceDetail, highlightStepKeys)) {
            matches.add(cell.index)
          }
        }
      }
    }
    return matches
  }, [highlightStepKeys, turns])
  const timelineEmphasis = searchMatchIndexes ?? highlightIndexes
  const selectionListenerRef = useRef(onTimelineSelectionChange)
  selectionListenerRef.current = onTimelineSelectionChange
  const hasSelectionListener = onTimelineSelectionChange !== undefined
  useEffect(() => {
    if (!hasSelectionListener) return
    if (timelineFocusIndexes === null) {
      selectionListenerRef.current?.(null)
      return
    }
    let startMs = Number.POSITIVE_INFINITY
    let endMs = Number.NEGATIVE_INFINITY
    for (const turn of turns) {
      for (const group of turn.groups) {
        for (const cell of group.cells) {
          if (!timelineFocusIndexes.has(cell.index)) continue
          const started = cell.startedAt
          if (started === null || started === undefined || !Number.isFinite(started)) continue
          const seconds = cell.timeSeconds
          const duration = typeof seconds === 'number' && Number.isFinite(seconds)
            ? Math.max(0, seconds * 1_000)
            : 0
          startMs = Math.min(startMs, started)
          endMs = Math.max(endMs, started + duration)
        }
      }
    }
    selectionListenerRef.current?.(
      Number.isFinite(startMs) && Number.isFinite(endMs) ? { startMs, endMs } : null,
    )
  }, [hasSelectionListener, timelineFocusIndexes, turns])
  const clearTokenRef = useRef(clearSelectionToken)
  useEffect(() => {
    if (clearTokenRef.current === clearSelectionToken) return
    clearTokenRef.current = clearSelectionToken
    setTimelineRange(null)
  }, [clearSelectionToken])
  const collapsibleTurnIds = useMemo(
    () => turns.flatMap(turn => turn.turn !== null && turn.groups.reduce(
      (count, group) => count + group.cells.filter(cell =>
        cell.requestOnly !== true && cell.kind !== 'system').length,
      0,
    ) > 1 ? [turn.turn] : []),
    [turns],
  )
  const collapsibleAssistantIds = useMemo(() => {
    const ids: string[] = []
    for (const turn of turns) {
      const cells = turn.groups.flatMap(group => group.cells)
      for (let index = 0; index < cells.length; index++) {
        const cell = cells[index]
        const next = cells[index + 1]
        if (cell?.kind === 'message' && (next?.kind === 'tool' || next?.kind === 'subtool')) {
          ids.push(trajectoryRecordId(cell))
        }
      }
    }
    return ids
  }, [turns])
  const allTurnsCollapsed = collapsibleTurnIds.length > 0
    && collapsibleTurnIds.every(turn => collapsedTurns.has(turn))
  const allAssistantsCollapsed = collapsibleAssistantIds.length > 0
    && collapsibleAssistantIds.every(id => collapsedAssistants.has(id))
  const displayedCollapsedTurns = useMemo<ReadonlySet<number>>(() => {
    if (viewState === undefined) return collapsedTurns
    if (!viewState.turnsCollapsed) return EMPTY_TURN_IDS
    const next = new Set(collapsibleTurnIds)
    for (const turn of revealedTurns) next.delete(turn)
    return next
  }, [collapsedTurns, collapsibleTurnIds, revealedTurns, viewState])
  const displayedCollapsedAssistants = useMemo<ReadonlySet<string>>(() => {
    if (viewState === undefined) return collapsedAssistants
    if (!viewState.callsCollapsed) return EMPTY_RECORD_IDS
    const next = new Set(collapsibleAssistantIds)
    for (const id of revealedAssistants) next.delete(id)
    return next
  }, [collapsedAssistants, collapsibleAssistantIds, revealedAssistants, viewState])
  useEffect(() => {
    if (viewState?.turnsCollapsed !== true && revealedTurns.size > 0) {
      setRevealedTurns(EMPTY_TURN_IDS)
    }
  }, [revealedTurns.size, viewState?.turnsCollapsed])
  useEffect(() => {
    if (viewState?.callsCollapsed !== true && revealedAssistants.size > 0) {
      setRevealedAssistants(EMPTY_RECORD_IDS)
    }
  }, [revealedAssistants.size, viewState?.callsCollapsed])
  const toggleTurn = useCallback((turn: number) => {
    if (viewState?.turnsCollapsed === true) {
      setRevealedTurns((current) => {
        const next = new Set(current)
        if (next.has(turn)) next.delete(turn)
        else next.add(turn)
        return next
      })
      return
    }
    setCollapsedTurns((current) => {
      const next = new Set(current)
      if (next.has(turn)) next.delete(turn)
      else next.add(turn)
      return next
    })
  }, [viewState?.turnsCollapsed])
  const toggleAssistant = useCallback((id: string) => {
    if (viewState?.callsCollapsed === true) {
      setRevealedAssistants((current) => {
        const next = new Set(current)
        if (next.has(id)) next.delete(id)
        else next.add(id)
        return next
      })
      return
    }
    setCollapsedAssistants((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [viewState?.callsCollapsed])
  const toggleAllTurns = useCallback(() => {
    setCollapsedTurns((current) => {
      const next = new Set(current)
      for (const turn of collapsibleTurnIds) {
        if (allTurnsCollapsed) next.delete(turn)
        else next.add(turn)
      }
      return next
    })
  }, [allTurnsCollapsed, collapsibleTurnIds])
  const toggleAllAssistants = useCallback(() => {
    setCollapsedAssistants((current) => {
      const next = new Set(current)
      for (const id of collapsibleAssistantIds) {
        if (allAssistantsCollapsed) next.delete(id)
        else next.add(id)
      }
      return next
    })
  }, [allAssistantsCollapsed, collapsibleAssistantIds])
  const rootStyle: ExplorerStyle = {
    '--trajectory-bottom-inset': typeof bottomInset === 'number'
      ? `${bottomInset}px`
      : bottomInset,
  }

  return (
    <TrajectoryThemeProvider colorMode={colorMode}>
      <div
        className={[css.root, 'jiuwenTrajectoryTheme', className].filter(Boolean).join(' ')}
        data-trajectory-theme={colorMode}
        data-display-mode={displayMode}
        style={rootStyle}
      >
      {displayMode !== 'records' ? (
        <>
          {showToolbar ? <TrajectoryToolbar
            showViewControls={showToolbarViewControls}
            actualDuration={actualDuration}
            onActualDurationChange={(value) => {
              setActualDuration(value)
              setTokenView(false)
              setTimelineRange(null)
            }}
            actualTime={actualTime}
            onActualTimeChange={(value) => { setActualTime(value); setTimelineRange(null) }}
            tokenView={tokenView}
            onTokenViewChange={(value) => { setTokenView(value); setTimelineRange(null) }}
            allTurnsCollapsed={allTurnsCollapsed}
            onToggleAllTurns={toggleAllTurns}
            allAssistantsCollapsed={allAssistantsCollapsed}
            onToggleAllAssistants={toggleAllAssistants}
            showSearch={showSearch}
            searchQuery={searchQuery}
            onSearchQueryChange={controlledSearchQuery === undefined ? setLocalSearchQuery : () => {}}
            afterActions={toolbarAddon}
            t={t}
          /> : null}
          <TrajectoryTimeline
            turns={turns}
            mode={timelineMode}
            range={timelineRange}
            selectedIndex={selectedTimelineIndex}
            searchMatchIndexes={timelineEmphasis}
            pinnedIndexes={highlightIndexes}
            onRangeChange={setTimelineRange}
            sharedDomain={sharedTimeDomain}
            {...(mapSharedTime === undefined ? {} : { mapTime: mapSharedTime })}
            lockViewport={lockTimelineViewport}
            spanSelectsRange={spanSelectsRange}
            clickSelectsRecord={clickSelectsRecord}
            sharedViewport={sharedViewport}
            {...(onSharedViewportChange === undefined ? {} : { onViewportChange: onSharedViewportChange })}
            {...(sharedViewportRef === undefined ? {} : { sharedViewportRef })}
            rangeAppearance={timelineRangeAppearance}
            onRecordSelect={(index) => {
              preferRecordScrollRef.current = true
              if (!lockTimelineViewport) setTimelineRange(null)
              setRecordSelection({ index })
              setSelectedTimelineIndex(index)
              onOverviewActivate?.()
            }}
            onRecordFocus={(index) => {
              preferRecordScrollRef.current = true
              setRecordFocus({ index })
              onOverviewActivate?.()
            }}
            nowMilliseconds={liveNowMilliseconds}
          />
        </>
      ) : null}
      {error !== null && (
        <div className={css.error} role="status">{error}</div>
      )}
      {displayMode !== 'overview' ? <div className={css.ledger}>
        <TrajectoryTable
          turns={turns}
          scrollToEndSignal={scrollToEndSignal}
          {...(requests === undefined ? {} : { requestNumbers: requests })}
          timelineFocusIndexes={timelineFocusIndexes}
          searchMatchIndexes={searchMatchIndexes}
          onSelectedIndexChange={setSelectedTimelineIndex}
          onRecordSelect={(index) => {
            if (timelineFocusIndexes !== null && !timelineFocusIndexes.has(index)) {
              setTimelineRange(null)
            }
          }}
          recordSelection={recordSelection}
          recordFocus={recordFocus}
          historyLoading={loading}
          onClearSelection={() => { if (!lockTimelineViewport) setTimelineRange(null) }}
          collapsedTurns={displayedCollapsedTurns}
          onToggleTurn={toggleTurn}
          collapsedAssistants={displayedCollapsedAssistants}
          onToggleAssistant={toggleAssistant}
          nowMilliseconds={liveNowMilliseconds}
        />
      </div> : null}
      </div>
    </TrajectoryThemeProvider>
  )
})
