/**
 * Stroke icons per document kind, tinted by platform; no emoji.
 *
 * Tints come from the theme's semantic tokens rather than the platforms' brand
 * hexes: the icon has to read on both themes, and a brand colour picked for a
 * white page does not. The glyph inside is drawn in the token for text on an
 * accent surface, which is what the tinted sheet is.
 */
const GLYPH = 'var(--color-action-primary-text)';

function tintFor(kind: string, provider: string): string {
  if (provider === 'feishu') return 'var(--color-action-primary)';
  if (kind === 'spreadsheet') return 'var(--color-feedback-success)';
  if (kind === 'presentation') return 'var(--color-feedback-warning)';
  if (kind === 'markdown') return 'var(--color-text-secondary)';
  return 'var(--color-feedback-info)';
}

export function KindIcon({ kind, provider, size = 14 }: { kind: string; provider: string; size?: number }) {
  const tint = tintFor(kind, provider);
  if (kind === 'markdown') {
    return (
      <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
        <rect x="3" y="5" width="18" height="14" rx="2" fill={tint} />
        <path d="M6 15V9l3 3 3-3v6M15 9v6M13 13l2 2 2-2" stroke={GLYPH} strokeWidth="1.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  const inner = kind === 'spreadsheet'
    ? <path d="M9 11h7v7H9zM9 14.5h7M12.5 11v7" stroke={GLYPH} strokeWidth="1.2" fill="none" />
    : kind === 'presentation'
      ? <rect x="9" y="12" width="7" height="5" stroke={GLYPH} strokeWidth="1.2" fill="none" />
      : <path d="M9 12h6M9 15h6M9 18h4" stroke={GLYPH} strokeWidth="1.5" />;
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <path d="M6 2h9l5 5v15H6z" fill={tint} />
      <path d="M15 2v5h5" fill={GLYPH} fillOpacity="0.5" />
      {inner}
    </svg>
  );
}
