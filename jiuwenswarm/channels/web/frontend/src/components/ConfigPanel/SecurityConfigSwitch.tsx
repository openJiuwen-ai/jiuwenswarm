export function SecurityConfigSwitch({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <div className="flex items-center border-b border-border px-4 py-2.5">
      <span className="w-[32%] shrink-0 text-xs text-text-muted">{label}</span>
      <div className="flex h-[calc(1.25rem+16px)] items-center pl-4">
        <button
          type="button"
          role="switch"
          aria-label={label}
          aria-checked={checked}
          onClick={() => onChange(!checked)}
          className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent focus-visible:outline-accent disabled:cursor-not-allowed ${checked ? 'bg-[var(--color-toggle-enabled)]' : 'bg-[var(--color-toggle-disabled)]'}`}
        >
          <span
            className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-[var(--color-control-thumb)] shadow ${checked ? 'translate-x-4' : 'translate-x-0'}`}
          />
        </button>
      </div>
    </div>
  );
}
