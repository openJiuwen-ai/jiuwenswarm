interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  title?: string;
}

export function Switch({ checked, onChange, disabled = false, title }: SwitchProps) {
  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onChange(!checked);
  };

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={handleClick}
      title={title}
      className={`
        relative inline-flex h-4 w-8 flex-shrink-0 cursor-pointer rounded-full 
        focus:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 items-center
        ${checked 
          ? 'bg-[var(--color-switch-track-on)]' 
          : 'bg-[var(--color-switch-track-off)]'}
        ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
      `}
    >
      <span
        className={`
          pointer-events-none inline-block h-3 w-3 transform rounded-full bg-[var(--color-control-thumb)] shadow

          ${checked ? 'translate-x-[18px]' : 'translate-x-0.5'}
        `}
      />
    </button>
  );
}
