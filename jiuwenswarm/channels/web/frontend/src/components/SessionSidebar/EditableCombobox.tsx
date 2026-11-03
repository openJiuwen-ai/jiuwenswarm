import { useEffect, useId, useMemo, useState, type FocusEvent, type KeyboardEvent } from 'react';
import {
  filterEditableComboboxOptions,
  resolveEditableComboboxCommit,
  type EditableComboboxOption,
} from './editableComboboxModel';

interface EditableComboboxProps {
  ariaLabel: string;
  disabled?: boolean;
  emptyText?: string;
  placeholder?: string;
  onChange: (value: string) => void;
  options: EditableComboboxOption[];
  value: string;
}

export function EditableCombobox({
  ariaLabel,
  disabled = false,
  emptyText = '没有匹配项',
  placeholder,
  onChange,
  options,
  value,
}: EditableComboboxProps) {
  const listboxId = useId();
  const selectedOption = options.find(option => option.value === value);
  const selectedLabel = selectedOption?.label ?? (value ? value : '');
  const [inputValue, setInputValue] = useState(selectedLabel);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const filteredOptions = useMemo(() => filterEditableComboboxOptions(options, inputValue, selectedLabel), [inputValue, options, selectedLabel]);

  useEffect(() => {
    if (!open) setInputValue(selectedLabel);
  }, [open, selectedLabel]);

  useEffect(() => {
    setActiveIndex(index => Math.min(index, Math.max(filteredOptions.length - 1, 0)));
  }, [filteredOptions.length]);

  const reset = () => {
    setOpen(false);
    setInputValue(selectedLabel);
    setActiveIndex(0);
  };

  const commitInput = () => {
    const committedValue = resolveEditableComboboxCommit(options, inputValue);
    setOpen(false);
    setActiveIndex(0);
    if (!committedValue || committedValue === value) {
      setInputValue(selectedLabel);
      return;
    }
    const committedOption = options.find(option => option.value === committedValue);
    setInputValue(committedOption?.label ?? committedValue);
    onChange(committedValue);
  };

  const selectOption = (option: EditableComboboxOption) => {
    setOpen(false);
    setInputValue(option.label);
    setActiveIndex(0);
    // 显式点选始终通知父组件：自定义生效后 value 可能仍停在某项，
    // 再点同一项时也需要触发切换回授权 Agent。
    onChange(option.value);
  };

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) commitInput();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setOpen(true);
      setActiveIndex(index => Math.min(index + 1, Math.max(filteredOptions.length - 1, 0)));
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      setOpen(true);
      setActiveIndex(index => Math.max(index - 1, 0));
      return;
    }
    if (event.key === 'Enter' && open) {
      event.preventDefault();
      if (filteredOptions.length > 0) selectOption(filteredOptions[activeIndex] ?? filteredOptions[0]);
      else commitInput();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      reset();
    }
  };

  return (
    <div className={`editable-combobox${open ? ' editable-combobox--open' : ''}`} onBlur={handleBlur}>
      <input
        type="text"
        role="combobox"
        aria-label={ariaLabel}
        aria-autocomplete="list"
        aria-controls={listboxId}
        aria-expanded={open}
        aria-activedescendant={open && filteredOptions.length > 0 ? `${listboxId}-option-${activeIndex}` : undefined}
        autoComplete="off"
        disabled={disabled}
        placeholder={placeholder}
        value={inputValue}
        onChange={event => {
          setInputValue(event.target.value);
          setOpen(true);
          setActiveIndex(0);
        }}
        onFocus={event => {
          event.currentTarget.select();
          setOpen(true);
          setActiveIndex(0);
        }}
        onKeyDown={handleKeyDown}
      />
      <button
        type="button"
        className="editable-combobox__toggle"
        aria-label={`${ariaLabel}选项`}
        aria-expanded={open}
        disabled={disabled}
        onClick={() => {
          setInputValue(selectedLabel);
          setOpen(current => !current);
          setActiveIndex(0);
        }}
      >
        <svg viewBox="0 0 16 16" aria-hidden>
          <path d="m4 6 4 4 4-4" />
        </svg>
      </button>
      {open && !disabled && (
        <div id={listboxId} className="editable-combobox__list" role="listbox">
          {filteredOptions.length > 0 ? (
            filteredOptions.map((option, index) => (
              <button
                id={`${listboxId}-option-${index}`}
                key={option.value}
                type="button"
                role="option"
                aria-selected={option.value === value}
                className={`editable-combobox__option${index === activeIndex ? ' editable-combobox__option--active' : ''}`}
                onMouseDown={event => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => selectOption(option)}
              >
                {option.label}
              </button>
            ))
          ) : (
            <div className="editable-combobox__empty">{emptyText}</div>
          )}
        </div>
      )}
    </div>
  );
}
