/**
 * 主页欢迎区的品牌位置：欢迎语与吉祥物（Gateway S_08 第 3.3 节）。
 * 外观插件可以替换品牌名、后半句与吉祥物；没有提供者或图片加载失败时显示宿主默认。
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore, type CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';
import beeFlyingIcon from '../../assets/bee-flying.webp';
import beeStaticIcon from '../../assets/bee-static.png';
import {
  DEFAULT_BRAND_NAME, parseColor, readableHighlight, useAppearancePart, useBrandIdentity, welcomeSuffix,
} from '../../features/webui';

export const BEE_ANIMATION_DURATION = 4536;

const THEME_ATTRIBUTES = ['data-theme', 'data-color-mode', 'data-webui-appearance', 'data-webui-surface'];
const WHITE: [number, number, number] = [255, 255, 255];

function subscribeTheme(listener: () => void): () => void {
  if (typeof MutationObserver === 'undefined' || typeof document === 'undefined') return () => undefined;
  const observer = new MutationObserver(listener);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: THEME_ATTRIBUTES });
  return () => observer.disconnect();
}

/** The page surface the welcome line sits on; it changes with the theme, colour mode and appearance. */
function pageBackground(): string {
  const root = document.documentElement;
  const value = getComputedStyle(root).getPropertyValue('--color-surface-page').trim();
  return value || (root.getAttribute('data-color-mode') === 'dark' ? '#111111' : '#ffffff');
}

/** Colour or two-stop gradient for the highlighted text, kept readable on the current page (Gateway S_08 section 3.3). */
function useHighlightStyle(highlight: readonly string[] | undefined): CSSProperties | undefined {
  const background = useSyncExternalStore(subscribeTheme, pageBackground, () => '#ffffff');
  return useMemo(() => {
    if (!highlight?.length) return undefined;
    const colors = readableHighlight(highlight, parseColor(background) ?? WHITE);
    if (colors.length === 1) return { color: colors[0] };
    if (colors.length !== 2) return undefined;
    return {
      backgroundImage: `linear-gradient(90deg, ${colors[0]}, ${colors[1]})`,
      WebkitBackgroundClip: 'text',
      backgroundClip: 'text',
      WebkitTextFillColor: 'transparent',
      color: 'transparent',
    };
  }, [highlight, background]);
}

/** Brand name plus a per-mode suffix; packages may replace either part (Gateway S_08 section 3.3). */
export function WelcomeHeading({ workMode }: { workMode: string }) {
  const { t, i18n } = useTranslation();
  const identity = useBrandIdentity();
  const welcome = useAppearancePart('welcome');
  const suffix = welcomeSuffix(welcome ?? undefined, workMode, i18n.language)
    ?? t(workMode === 'design' ? 'chat.welcomeSuffix.design' : 'chat.welcomeSuffix.work');
  const highlight = useHighlightStyle(welcome?.highlight);
  // The second half takes the same colour only when the package asks for the whole line.
  const suffixStyle = welcome?.highlightScope === 'line' ? highlight : undefined;

  return (
    <>
      <span className="chat-welcome__heading-highlight" data-testid="chat-panel-welcome-heading-brand"
        data-variant={highlight ? 'custom' : 'default'} style={highlight}>
        {identity?.name ?? DEFAULT_BRAND_NAME}
      </span>
      <span data-testid="chat-panel-welcome-heading-suffix" data-variant={suffixStyle ? 'custom' : 'default'}
        style={suffixStyle}>{suffix}</span>
    </>
  );
}

/** The home-page mascot: the host bee, or the appearance mascot part (Gateway S_08 section 3.3). */
export function WelcomeMascot({
  className,
  altText,
  onTrigger,
  onLeave,
}: {
  className: string;
  altText: string;
  onTrigger: () => void;
  onLeave: () => void;
}) {
  const [isPlaying, setIsPlaying] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // A package mascot replaces the bee; any image that fails to load brings the bee back (Gateway S_08 section 3.3).
  const mascot = useAppearancePart('mascot');
  const [failedMascot, setFailedMascot] = useState<string | null>(null);
  const custom = mascot && failedMascot !== mascot.src ? mascot : null;
  const hoverMs = custom ? custom.hoverMs ?? 0 : BEE_ANIMATION_DURATION;

  const handleMouseEnter = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    onTrigger();
    if (hoverMs <= 0) return;
    setIsPlaying(true);
    timerRef.current = setTimeout(() => {
      setIsPlaying(false);
      timerRef.current = null;
    }, hoverMs);
  }, [onTrigger, hoverMs]);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  // A different mascot starts still, whatever the previous one was doing.
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = null;
    setIsPlaying(false);
  }, [custom?.src, custom?.hoverSrc]);

  const src = custom
    ? (isPlaying && custom.hoverSrc ? custom.hoverSrc : custom.src)
    : (isPlaying ? beeFlyingIcon : beeStaticIcon);
  return (
    <img
      className={custom ? `${className} chat-welcome__banner--mascot` : className}
      src={src}
      alt={altText}
      data-testid="chat-panel-welcome-banner"
      data-variant={custom ? 'custom' : 'default'}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={onLeave}
      onError={custom ? () => setFailedMascot(custom.src) : undefined}
    />
  );
}
