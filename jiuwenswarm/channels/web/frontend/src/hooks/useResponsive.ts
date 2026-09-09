import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react';
import { breakpoints, canFitBoth, canFitToolPanelOnly, type BreakpointKey } from '../styles/breakpoints';

/* ── 基础：通用媒体查询 ── */

export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === 'undefined') return false;
    return window.matchMedia(query).matches;
  });

  useEffect(() => {
    const mql = window.matchMedia(query);
    const handler = (e: MediaQueryListEvent) => setMatches(e.matches);
    setMatches(mql.matches);
    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, [query]);

  return matches;
}

export function useMaxWidth(key: BreakpointKey): boolean {
  return useMediaQuery(`(max-width: ${breakpoints[key]}px)`);
}

export function useMinWidth(key: BreakpointKey): boolean {
  return useMediaQuery(`(min-width: ${breakpoints[key]}px)`);
}

/* ── 业务：侧边栏 / 工具面板布局状态 ── */

export function useResponsiveLayout() {
  const isMobile = useMaxWidth('sm');
  const [conversationSidebarCollapsed, setConversationSidebarCollapsed] = useState(false);
  const [conversationSidebarFloating, setConversationSidebarFloating] = useState(false);
  const [toolPanelHidden, setToolPanelHidden] = useState(false);

  useEffect(() => {
    if (isMobile) {
      setConversationSidebarCollapsed(true);
      setConversationSidebarFloating(true);
    } else {
      setConversationSidebarFloating(false);
    }
  }, [isMobile]);

  return {
    isMobile,
    conversationSidebarCollapsed,
    setConversationSidebarCollapsed,
    conversationSidebarFloating,
    setConversationSidebarFloating,
    toolPanelHidden,
    setToolPanelHidden,
  };
}

/* ── 业务：面板互斥 / 全屏判定 ── */

export interface ResponsivePanelResizeParams {
  isTeamAreaExpanded: boolean;
  conversationSidebarCollapsed: boolean;
  setConversationSidebarCollapsed: (collapsed: boolean) => void;
  setSingleAgentPanelExpanded: (expanded: boolean) => void;
  setTeamAreaExpanded: (expanded: boolean) => void;
  mode: string;
}

export function useResponsivePanelResize({
  isTeamAreaExpanded,
  conversationSidebarCollapsed,
  setConversationSidebarCollapsed,
  setSingleAgentPanelExpanded,
  setTeamAreaExpanded,
  mode,
}: ResponsivePanelResizeParams) {
  const [shouldFullscreen, setShouldFullscreen] = useState(false);
  const stateRef = useRef({ isTeamAreaExpanded, conversationSidebarCollapsed, mode });
  stateRef.current = { isTeamAreaExpanded, conversationSidebarCollapsed, mode };
  const prevRef = useRef({ isTeamAreaExpanded, conversationSidebarCollapsed });

  useLayoutEffect(() => {
    if (!isTeamAreaExpanded) {
      setShouldFullscreen(false);
      prevRef.current = { isTeamAreaExpanded, conversationSidebarCollapsed };
      return;
    }

    const prev = prevRef.current;
    const justExpanded = isTeamAreaExpanded && !prev.isTeamAreaExpanded;
    prevRef.current = { isTeamAreaExpanded, conversationSidebarCollapsed };

    const check = (isUserAction: boolean) => {
      const s = stateRef.current;
      if (!canFitToolPanelOnly()) {
        if (isUserAction) {
          setShouldFullscreen(true);
        } else if (s.isTeamAreaExpanded && !shouldFullscreen) {
          if (s.mode === 'team') {
            setTeamAreaExpanded(false);
          } else {
            setSingleAgentPanelExpanded(false);
          }
        }
        return;
      }
      setShouldFullscreen(false);
      if (!canFitBoth() && !s.conversationSidebarCollapsed) {
        setConversationSidebarCollapsed(true);
      }
    };

    check(justExpanded);
    const onResize = () => check(false);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [isTeamAreaExpanded, shouldFullscreen, setConversationSidebarCollapsed, setSingleAgentPanelExpanded, setTeamAreaExpanded]);

  useEffect(() => {
    if (shouldFullscreen) {
      prevRef.current = { isTeamAreaExpanded, conversationSidebarCollapsed };
      return;
    }
    if (canFitBoth()) {
      prevRef.current = { isTeamAreaExpanded, conversationSidebarCollapsed };
      return;
    }

    const prev = prevRef.current;
    const teamAreaJustExpanded = isTeamAreaExpanded && !prev.isTeamAreaExpanded;
    const sidebarJustExpanded = !conversationSidebarCollapsed && prev.conversationSidebarCollapsed;

    if (teamAreaJustExpanded && !conversationSidebarCollapsed) {
      setConversationSidebarCollapsed(true);
    } else if (sidebarJustExpanded && isTeamAreaExpanded) {
      if (mode === 'team') {
        setTeamAreaExpanded(false);
      } else {
        setSingleAgentPanelExpanded(false);
      }
    }

    prevRef.current = { isTeamAreaExpanded, conversationSidebarCollapsed };
  }, [shouldFullscreen, isTeamAreaExpanded, conversationSidebarCollapsed, mode, setConversationSidebarCollapsed, setSingleAgentPanelExpanded, setTeamAreaExpanded]);

  return { shouldFullscreen };
}

/* ── 业务：Welcome 气泡定位 ── */

export interface WelcomeBubblePositionParams {
  panelRef: RefObject<HTMLDivElement>;
  bubbleRef: RefObject<HTMLDivElement>;
  active: boolean;
}

const BUBBLE_RIGHT_BREAKPOINTS: Array<{ minWidth: number; right: number }> = [
  { minWidth: 1130, right: -114 },
  { minWidth: 1000, right: -55 },
  { minWidth: 800, right: -10 },
];

const BUBBLE_DEFAULT_RIGHT = -10;

export function useWelcomeBubblePosition({ panelRef, bubbleRef, active }: WelcomeBubblePositionParams) {
  useEffect(() => {
    if (!active) return;
    const panel = panelRef.current;
    if (!panel || typeof ResizeObserver === 'undefined') return;

    const updateBubbleRight = () => {
      const bubble = bubbleRef.current;
      if (!bubble) return;
      const width = panel.offsetWidth;
      const matched = BUBBLE_RIGHT_BREAKPOINTS.find((bp) => width >= bp.minWidth);
      const rightValue = matched ? matched.right : BUBBLE_DEFAULT_RIGHT;
      bubble.style.right = `${rightValue}px`;
    };

    const raf = requestAnimationFrame(updateBubbleRight);

    const observer = new ResizeObserver(updateBubbleRight);
    observer.observe(panel);
    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
    };
  }, [panelRef, bubbleRef, active]);
}


