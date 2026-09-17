import SearchIcon from '../../assets/agent-management/agent-search.svg?react';

interface PickerSearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  inputTestId: string;
  wrapperTestId?: string;
}

export function PickerSearchInput({
  value,
  onChange,
  placeholder,
  inputTestId,
  wrapperTestId,
}: PickerSearchInputProps) {
  const showClear = value.length > 0;

  return (
    <div className="chat-picker-panel__search" data-testid={wrapperTestId}>
      <div className="chat-picker-panel__search-inner">
        <SearchIcon aria-hidden="true" />
        <input
          type="text"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={placeholder}
          data-testid={inputTestId}
        />
        {showClear && (
          <button
            type="button"
            className="chat-picker-panel__search-clear"
            aria-label="clear"
            data-testid={`${inputTestId.replace(/-input$/, '')}-clear`}
            onClick={() => onChange('')}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
