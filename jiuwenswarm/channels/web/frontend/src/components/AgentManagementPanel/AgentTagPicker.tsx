import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { AGENT_TAG_OPTIONS } from '../../features/agentManagement/tagOptions';
import PlusIcon from '../../assets/agent-management/agent-plus.svg?react';

type AgentTagPickerProps = {
  tagIds: string[];
  customTags: string[];
  label: string;
  placeholder: string;
  onChange: (value: { tagIds: string[]; customTags: string[] }) => void;
};

export function AgentTagPicker({ tagIds, customTags, label, placeholder, onChange }: AgentTagPickerProps) {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const [customTagInput, setCustomTagInput] = useState('');
  const [canScrollLeft, setCanScrollLeft] = useState(false);
  const [canScrollRight, setCanScrollRight] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const valuesRef = useRef<HTMLSpanElement>(null);
  const valueSignature = `${tagIds.join('\u0000')}\u0001${customTags.join('\u0000')}`;

  const updateScrollState = useCallback(() => {
    const values = valuesRef.current;
    if (!values) return;
    setCanScrollLeft(values.scrollLeft > 1);
    setCanScrollRight(values.scrollLeft < values.scrollWidth - values.clientWidth - 1);
  }, []);

  useLayoutEffect(() => {
    const values = valuesRef.current;
    if (!values) return;
    updateScrollState();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(updateScrollState);
    observer.observe(values);
    return () => observer.disconnect();
  }, [updateScrollState, valueSignature]);

  useEffect(() => {
    if (!menuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [menuOpen]);

  const update = (nextTagIds: string[], nextCustomTags = customTags) =>
    onChange({ tagIds: nextTagIds, customTags: nextCustomTags });
  const toggleTag = (tagId: string) =>
    update(tagIds.includes(tagId) ? tagIds.filter((id) => id !== tagId) : [...tagIds, tagId]);
  const addCustomTag = () => {
    const value = customTagInput.trim();
    if (!value || customTags.includes(value)) return;
    update(tagIds, [...customTags, value]);
    setCustomTagInput('');
  };
  const removeCustomTag = (tag: string) =>
    update(
      tagIds,
      customTags.filter((item) => item !== tag),
    );
  const scrollValues = (direction: 1 | -1) =>
    valuesRef.current?.scrollBy({ left: direction * (valuesRef.current.clientWidth * 0.8), behavior: 'smooth' });
  const isEmpty = tagIds.length === 0 && customTags.length === 0;

  return (
    <div className="agent-management-tag-picker" ref={pickerRef} data-testid="agent-tag-picker">
      <div
        className="agent-management-tag-picker__trigger"
        data-testid="agent-tag-picker-trigger"
        data-empty={isEmpty}
        onClick={(event) => {
          if ((event.target as HTMLElement).closest('button')) return;
          setMenuOpen((open) => !open);
        }}
      >
        <button
          type="button"
          className="agent-management-tag-picker__scroll agent-management-tag-picker__scroll--prev"
          data-testid="agent-tag-picker-scroll-prev"
          aria-label={t('agentManagement.form.tagScrollPrev')}
          data-hidden={!canScrollLeft}
          onClick={(event) => {
            event.stopPropagation();
            scrollValues(-1);
          }}
        >
          <ChevronLeft size={16} aria-hidden="true" />
        </button>
        <span ref={valuesRef} className="agent-management-tag-picker__values" onScroll={updateScrollState}>
          {!isEmpty ? (
            <>
              {tagIds.map((tagId) => {
                const option = AGENT_TAG_OPTIONS.find((item) => item.id === tagId);
                if (!option) return null;
                const tagLabel = t(option.labelKey);
                return (
                  <span key={tagId} className="agent-management-tag agent-management-tag--selected">
                    <span>{tagLabel}</span>
                    <button
                      type="button"
                      aria-label={t('agentManagement.form.removeTag', { name: tagLabel })}
                      data-testid="agent-tag-picker-remove-tag"
                      data-variant={tagId}
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleTag(tagId);
                      }}
                    >
                      <span aria-hidden="true">×</span>
                    </button>
                  </span>
                );
              })}
              {customTags.map((tag) => (
                <span key={tag} className="agent-management-tag agent-management-tag--selected">
                  <span>{tag}</span>
                  <button
                    type="button"
                    aria-label={t('agentManagement.form.removeCustomTag', { name: tag })}
                    data-testid="agent-tag-picker-remove-custom-tag"
                    data-variant={tag}
                    onClick={(event) => {
                      event.stopPropagation();
                      removeCustomTag(tag);
                    }}
                  >
                    <span aria-hidden="true">×</span>
                  </button>
                </span>
              ))}
            </>
          ) : (
            <span className="agent-management-form-placeholder">{placeholder}</span>
          )}
        </span>
        <button
          type="button"
          className="agent-management-tag-picker__scroll agent-management-tag-picker__scroll--next"
          data-testid="agent-tag-picker-scroll-next"
          aria-label={t('agentManagement.form.tagScrollNext')}
          data-hidden={!canScrollRight}
          onClick={(event) => {
            event.stopPropagation();
            scrollValues(1);
          }}
        >
          <ChevronRight size={16} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="agent-management-tag-picker__toggle"
          data-testid="agent-tag-picker-toggle"
          aria-label={t('agentManagement.form.toggleTags')}
          aria-expanded={menuOpen}
          aria-haspopup="listbox"
          onClick={(event) => {
            event.stopPropagation();
            setMenuOpen((open) => !open);
          }}
        >
          <ChevronDown className="agent-management-tag-picker__chevron" size={16} aria-hidden="true" />
        </button>
      </div>
      {menuOpen ? (
        <div
          className="agent-management-tag-picker__options"
          role="listbox"
          aria-label={label}
          data-testid="agent-tag-picker-options"
        >
          {AGENT_TAG_OPTIONS.map((option) => {
            const selected = tagIds.includes(option.id);
            return (
              <button
                key={option.id}
                type="button"
                role="option"
                aria-selected={selected}
                data-testid="agent-tag-picker-option"
                data-variant={option.id}
                className={selected ? 'is-selected' : ''}
                onClick={() => toggleTag(option.id)}
              >
                <span>{t(option.labelKey)}</span>
                {selected ? <Check size={14} aria-hidden="true" /> : null}
              </button>
            );
          })}
          <div className="agent-management-tag-picker__custom">
            <input
              data-testid="agent-tag-picker-custom-input"
              value={customTagInput}
              onChange={(event) => setCustomTagInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  addCustomTag();
                }
              }}
              placeholder={t('agentManagement.form.customTagPlaceholder')}
              aria-label={t('agentManagement.form.customTagPlaceholder')}
            />
            <button
              type="button"
              data-testid="agent-tag-picker-add-custom"
              onClick={addCustomTag}
              disabled={!customTagInput.trim()}
            >
              <PlusIcon aria-hidden="true" />
              {t('agentManagement.form.addCustomTag')}
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
