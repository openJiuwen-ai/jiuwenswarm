import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { useTranslation } from 'react-i18next';
import { CloseButton } from '../../../components/ui/CloseButton/CloseButton.tsx';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  type DropdownMenuAlign,
  type DropdownMenuSide,
} from '../../../components/ui/DropdownMenu/DropdownMenu.tsx';
import type { PresentationDeclaration } from './hostContext.ts';
import {
  clampLauncherPosition,
  getLauncherMenuPlacement,
  LAUNCHER_DRAG_THRESHOLD_PX,
  readStoredLauncherPosition,
  reconcileDismissedKeys,
  sameKeySet,
  writeStoredLauncherPosition,
  type LauncherPosition,
} from './launcherState.ts';
import './PresentationLauncher.css';

export interface PresentationLauncherProps {
  connected: boolean;
  opening: boolean;
  desktopAvailable: boolean;
  candidates: readonly PresentationDeclaration[];
  onActivate: (declaration: PresentationDeclaration) => void;
  onMenuOpen: () => void;
}

export function PresentationLauncher({
  connected,
  opening,
  desktopAvailable,
  candidates,
  onActivate,
  onMenuOpen,
}: PresentationLauncherProps) {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPlacement, setMenuPlacement] = useState<{ side: DropdownMenuSide; align: DropdownMenuAlign }>({
    side: 'top',
    align: 'end',
  });
  const [dragging, setDragging] = useState(false);
  const [alignStart, setAlignStart] = useState(false);
  const savedPositionRef = useRef<LauncherPosition | null>(readStoredLauncherPosition());
  const [position, setPosition] = useState<LauncherPosition | null>(() => {
    const stored = savedPositionRef.current;
    if (!stored || typeof window === 'undefined') return stored;
    return clampLauncherPosition(stored, { width: 0, height: 0 }, { width: window.innerWidth, height: window.innerHeight });
  });
  const containerRef = useRef<HTMLDivElement>(null);
  const suppressNextClickRef = useRef(false);
  const clickResetTimerRef = useRef<number | null>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);

  const candidateKeys = useMemo(() => candidates.map((item) => item.key), [candidates]);
  const [dismissedKeys, setDismissedKeys] = useState<Set<string>>(() => new Set());
  const activeDismissedKeys = useMemo(
    () => reconcileDismissedKeys(dismissedKeys, candidateKeys),
    [dismissedKeys, candidateKeys],
  );
  useEffect(() => {
    if (!sameKeySet(dismissedKeys, activeDismissedKeys)) {
      setDismissedKeys(activeDismissedKeys);
    }
  }, [dismissedKeys, activeDismissedKeys]);

  const hidden = candidates.length === 0 || candidates.every((item) => activeDismissedKeys.has(item.key));

  useEffect(() => {
    if (hidden) {
      dragCleanupRef.current?.();
      if (menuOpen) {
        setMenuOpen(false);
      }
    }
  }, [hidden, menuOpen]);

  useEffect(() => () => {
    dragCleanupRef.current?.();
    if (clickResetTimerRef.current !== null) {
      window.clearTimeout(clickResetTimerRef.current);
    }
  }, []);

  const syncClampedPosition = useCallback(() => {
    if (!savedPositionRef.current || !containerRef.current) {
      setAlignStart(false);
      return;
    }
    const rect = containerRef.current.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const next = clampLauncherPosition(
      savedPositionRef.current,
      { width: rect.width, height: rect.height },
      { width: viewportWidth, height: window.innerHeight },
    );
    setPosition((prev) => (prev && prev.x === next.x && prev.y === next.y ? prev : next));
    setAlignStart(next.x + rect.width / 2 < viewportWidth / 2);
  }, []);

  useLayoutEffect(() => {
    if (hidden) return;
    syncClampedPosition();
    window.addEventListener('resize', syncClampedPosition);
    return () => window.removeEventListener('resize', syncClampedPosition);
  }, [hidden, candidates.length, syncClampedPosition]);

  const resolvePlacement = useCallback(() => {
    const rect = containerRef.current?.getBoundingClientRect();
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    if (rect && (rect.width > 0 || rect.height > 0)) {
      return getLauncherMenuPlacement(rect, viewport);
    }
    if (position) {
      return getLauncherMenuPlacement({ left: position.x, top: position.y, width: 0, height: 0 }, viewport);
    }
    return { side: 'top' as const, align: 'end' as const };
  }, [position]);

  const handleOpenChange = useCallback((open: boolean) => {
    if (open) {
      setMenuPlacement(resolvePlacement());
      onMenuOpen();
    }
    setMenuOpen(open);
  }, [onMenuOpen, resolvePlacement]);

  const handlePointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const container = containerRef.current;
    if (event.button !== 0 || !container) return;
    dragCleanupRef.current?.();
    if (clickResetTimerRef.current !== null) {
      window.clearTimeout(clickResetTimerRef.current);
      clickResetTimerRef.current = null;
    }
    suppressNextClickRef.current = false;

    const rect = container.getBoundingClientRect();
    const origin = position ?? { x: rect.left, y: rect.top };
    const startX = event.clientX;
    const startY = event.clientY;
    const pointerId = event.pointerId;
    let moved = false;
    let captured = false;
    let latestPosition = origin;

    const handlePointerMove = (moveEvent: PointerEvent) => {
      if (pointerId !== undefined && moveEvent.pointerId !== undefined && moveEvent.pointerId !== pointerId) return;
      const dx = moveEvent.clientX - startX;
      const dy = moveEvent.clientY - startY;
      if (!moved) {
        if (Math.hypot(dx, dy) < LAUNCHER_DRAG_THRESHOLD_PX) return;
        moved = true;
        suppressNextClickRef.current = true;
        // Capture only after crossing the drag threshold so plain clicks still target the trigger button.
        if (pointerId !== undefined && typeof container.setPointerCapture === 'function') {
          try {
            container.setPointerCapture(pointerId);
            captured = true;
          } catch {
            // Ignore synthetic or already-ended pointers that cannot be captured.
          }
        }
        setDragging(true);
        setMenuOpen(false);
      }
      const viewportWidth = window.innerWidth;
      const next = clampLauncherPosition(
        { x: origin.x + dx, y: origin.y + dy },
        { width: rect.width, height: rect.height },
        { width: viewportWidth, height: window.innerHeight },
      );
      latestPosition = next;
      setPosition(next);
      setAlignStart(next.x + rect.width / 2 < viewportWidth / 2);
    };

    const cleanup = () => {
      window.removeEventListener('pointermove', handlePointerMove);
      window.removeEventListener('pointerup', handlePointerEnd);
      window.removeEventListener('pointercancel', handlePointerEnd);
      container.removeEventListener('lostpointercapture', handlePointerEnd);
      if (captured && pointerId !== undefined && typeof container.releasePointerCapture === 'function') {
        captured = false;
        try {
          if (typeof container.hasPointerCapture !== 'function' || container.hasPointerCapture(pointerId)) {
            container.releasePointerCapture(pointerId);
          }
        } catch {
          // Ignore if the browser already released pointer capture.
        }
      }
      setDragging(false);
      if (dragCleanupRef.current === cleanup) dragCleanupRef.current = null;
    };

    const handlePointerEnd = (endEvent: PointerEvent) => {
      if (pointerId !== undefined && endEvent.pointerId !== undefined && endEvent.pointerId !== pointerId) return;
      cleanup();
      if (moved) {
        savedPositionRef.current = latestPosition;
        writeStoredLauncherPosition(latestPosition);
        clickResetTimerRef.current = window.setTimeout(() => {
          suppressNextClickRef.current = false;
          clickResetTimerRef.current = null;
        }, 0);
      }
    };

    dragCleanupRef.current = cleanup;
    window.addEventListener('pointermove', handlePointerMove);
    window.addEventListener('pointerup', handlePointerEnd);
    window.addEventListener('pointercancel', handlePointerEnd);
    container.addEventListener('lostpointercapture', handlePointerEnd);
  }, [position]);

  const handleTriggerClick = useCallback((event: ReactMouseEvent<HTMLButtonElement>) => {
    if (!suppressNextClickRef.current) return;
    suppressNextClickRef.current = false;
    if (clickResetTimerRef.current !== null) {
      window.clearTimeout(clickResetTimerRef.current);
      clickResetTimerRef.current = null;
    }
    event.preventDefault();
    event.stopPropagation();
  }, []);

  const handleDismiss = useCallback(() => {
    setMenuOpen(false);
    setDismissedKeys(new Set(candidateKeys));
  }, [candidateKeys]);

  if (hidden) return null;

  const hasDesktopCandidate = candidates.some((item) => item.mode !== 'overlay');
  const hasOverlayCandidate = candidates.some((item) => item.mode === 'overlay');
  const openDisabled = !connected || opening || (!desktopAvailable && !hasOverlayCandidate);

  const style: CSSProperties | undefined = position
    ? { left: position.x, top: position.y, right: 'auto', bottom: 'auto' }
    : undefined;

  return (
    <div
      ref={containerRef}
      className={`webui-presentation-launcher${dragging ? ' webui-presentation-launcher--dragging' : ''}${alignStart ? ' webui-presentation-launcher--align-start' : ''}`}
      data-testid="webui-presentation-launcher"
      data-dragging={dragging ? 'true' : 'false'}
      style={style}
      onPointerDown={handlePointerDown}
    >
      <div className="webui-presentation-launcher__pill">
        <DropdownMenu open={menuOpen} onOpenChange={handleOpenChange}>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              className="webui-presentation-launcher__button"
              data-testid="webui-presentation-open"
              disabled={openDisabled}
              onClick={handleTriggerClick}
            >
              {opening ? t('webuiPresentation.opening') : t('webuiPresentation.choose')}
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side={menuPlacement.side} align={menuPlacement.align}>
            {candidates.map((declaration) => (
              <DropdownMenuItem
                key={declaration.key}
                testId="webui-presentation-launcher-item"
                data-variant={declaration.key}
                disabled={declaration.mode !== 'overlay' && !desktopAvailable}
                onSelect={() => onActivate(declaration)}
              >
                {declaration.title}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        <div
          className="webui-presentation-launcher__close"
          onPointerDown={(event) => event.stopPropagation()}
        >
          <CloseButton
            size={14}
            onClick={handleDismiss}
            ariaLabel={t('webuiPresentation.hideLauncher')}
            title={t('webuiPresentation.hideLauncher')}
            testId="webui-presentation-launcher-close"
          />
        </div>
      </div>
      {!desktopAvailable && hasDesktopCandidate
        ? <span data-testid="webui-presentation-desktop-required">{t('webuiPresentation.desktopRequired')}</span>
        : null}
    </div>
  );
}
