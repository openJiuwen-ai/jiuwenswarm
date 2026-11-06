interface JiuwenMateModeSwitchProps {
  active: boolean;
  label: string;
  onToggle: () => void;
  iconOnly?: boolean;
}

export function JiuwenMateModeSwitch({ active, label, onToggle, iconOnly = false }: JiuwenMateModeSwitchProps) {
  return (
    <button
      type="button"
      className={`icon-rail-nav-item icon-rail-nav-item--mode-switch${active ? ' icon-rail-nav-item--active' : ''}${
        iconOnly ? ' icon-rail-nav-item--icon-only' : ''
      }`}
      aria-pressed={active}
      aria-label={iconOnly ? label : undefined}
      title={iconOnly ? label : undefined}
      onClick={onToggle}
      data-testid="session-sidebar-jiuwenmate-switch"
    >
      <span className="icon-rail-nav-item__icon" aria-hidden>
        <svg viewBox="0 0 24 24" fill="none">
          <path d="M7 7h11m0 0-3-3m3 3-3 3M17 17H6m0 0 3 3m-3-3 3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </span>
      {iconOnly ? null : <span className="icon-rail-nav-item__label">{label}</span>}
    </button>
  );
}
