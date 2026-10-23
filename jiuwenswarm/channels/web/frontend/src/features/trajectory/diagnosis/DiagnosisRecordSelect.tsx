/** Drop-down of a session's diagnosis records: pick one, search, or delete one. */

import { memo, useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { ChevronDown, Search, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { isRunning } from './diagnosisState';
import type { DiagnosisSummary } from './diagnosisTypes';
import { filterRecords, formatRecordTime, recordIconKind, recordLabel } from './diagnosisView';
import { DiagnosisStatusIcon } from './DiagnosisStatusIcon';
import css from './DiagnosisRecordSelect.module.css';

export interface DiagnosisRecordSelectProps {
  /** Records of the session, newest first. */
  items: readonly DiagnosisSummary[];
  /** The record on screen. */
  currentId: string | null;
  onSelect: (diagnosisId: string) => void;
  onDelete: (diagnosisId: string) => void;
}

export const DiagnosisRecordSelect = memo(function DiagnosisRecordSelect({
  items,
  currentId,
  onSelect,
  onDelete,
}: DiagnosisRecordSelectProps) {
  const { t } = useTranslation();
  const listboxId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeIndex, setActiveIndex] = useState(0);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const scopeLabelOf = useCallback(
    (kind: DiagnosisSummary['scope_kind']) => t(`trajectory.diagnosis.scopeKind.${kind}`),
    [t],
  );
  const filtered = useMemo(() => filterRecords(items, query, scopeLabelOf), [items, query, scopeLabelOf]);
  const current = items.find(item => item.diagnosis_id === currentId) ?? null;
  const rowText = useCallback((item: DiagnosisSummary) => ({
    time: formatRecordTime(item.created_at),
    label: recordLabel(item, scopeLabelOf(item.scope_kind)),
  }), [scopeLabelOf]);

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    setQuery('');
    setConfirmingId(null);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  // Each time the list opens, search starts empty with the open record highlighted.
  const openPanel = () => {
    const index = items.findIndex(item => item.diagnosis_id === currentId);
    setActiveIndex(index < 0 ? 0 : index);
    setOpen(true);
  };
  useEffect(() => {
    if (open) searchRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current !== null && event.target instanceof Node && !rootRef.current.contains(event.target)) {
        close(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [close, open]);

  const choose = useCallback((item: DiagnosisSummary) => {
    onSelect(item.diagnosis_id);
    close(true);
  }, [close, onSelect]);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close(true);
      return;
    }
    if (filtered.length === 0) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      setActiveIndex(index => (index + step + filtered.length) % filtered.length);
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      setActiveIndex(event.key === 'Home' ? 0 : filtered.length - 1);
    } else if (event.key === 'Enter' && event.target === searchRef.current) {
      event.preventDefault();
      const item = filtered[Math.min(activeIndex, filtered.length - 1)];
      if (item !== undefined) choose(item);
    }
  };

  const currentText = current === null ? null : rowText(current);
  const empty = items.length === 0;
  const activeItem = filtered[Math.min(activeIndex, filtered.length - 1)];
  const captionId = `${listboxId}-caption`;

  return (
    <div className={css.root} ref={rootRef} data-testid="trajectory-diagnosis-record-select">
      <span className={css.caption} id={captionId} data-testid="trajectory-diagnosis-record-history">
        {t('trajectory.diagnosis.recordSelect.history')}
      </span>
      <div className={css.field}>
      <button
        ref={triggerRef}
        type="button"
        className={`${css.trigger} ${open ? css.triggerOpen : ''}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-labelledby={captionId}
        disabled={empty}
        onClick={() => (open ? close(false) : openPanel())}
        data-testid="trajectory-diagnosis-record-trigger"
      >
        {current === null || currentText === null ? (
          <span className={css.placeholder}>
            {empty ? t('trajectory.diagnosis.noRecords') : t('trajectory.diagnosis.recordSelect.placeholder')}
          </span>
        ) : (
          <>
            <DiagnosisStatusIcon kind={recordIconKind(current.status)} />
            <span className={css.srOnly}>{t(`trajectory.diagnosis.status.${current.status}`)}</span>
            <span className={css.time}>{currentText.time}</span>
            <span className={css.label} title={currentText.label}>{currentText.label}</span>
          </>
        )}
        <ChevronDown className={css.chevron} size={16} strokeWidth={2} aria-hidden />
      </button>
      {open ? (
        <div className={css.panel} onKeyDown={onKeyDown} data-testid="trajectory-diagnosis-record-panel">
          <label className={css.search}>
            <Search size={14} strokeWidth={2} aria-hidden />
            <input
              ref={searchRef}
              type="search"
              role="combobox"
              aria-expanded="true"
              aria-controls={listboxId}
              aria-activedescendant={activeItem === undefined ? undefined : `${listboxId}-${activeItem.diagnosis_id}`}
              aria-label={t('trajectory.diagnosis.recordSelect.search')}
              placeholder={t('trajectory.diagnosis.recordSelect.search')}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setActiveIndex(0);
                setConfirmingId(null);
              }}
              data-testid="trajectory-diagnosis-record-search"
            />
          </label>
          {filtered.length === 0 ? (
            <p className={css.empty} data-testid="trajectory-diagnosis-record-empty">
              {t('trajectory.diagnosis.recordSelect.noMatch')}
            </p>
          ) : (
            <ul
              id={listboxId}
              className={css.list}
              role="listbox"
              aria-label={t('trajectory.diagnosis.records')}
            >
              {filtered.map((item, index) => {
                const text = rowText(item);
                const selected = item.diagnosis_id === currentId;
                const running = isRunning(item);
                const confirming = confirmingId === item.diagnosis_id;
                return (
                  <li className={css.row} role="presentation" key={item.diagnosis_id}>
                    <div
                      id={`${listboxId}-${item.diagnosis_id}`}
                      role="option"
                      aria-selected={selected}
                      className={`${css.option} ${index === activeIndex ? css.optionActive : ''} ${
                        selected ? css.optionSelected : ''
                      }`}
                      onClick={() => choose(item)}
                      onMouseEnter={() => setActiveIndex(index)}
                      data-testid="trajectory-diagnosis-record-item"
                      data-selected={selected ? 'true' : 'false'}
                      data-status={item.status}
                    >
                      <DiagnosisStatusIcon kind={recordIconKind(item.status)} />
                      <span className={css.srOnly}>{t(`trajectory.diagnosis.status.${item.status}`)}</span>
                      <span className={css.time}>{text.time}</span>
                      <span className={css.label} title={text.label}>{text.label}</span>
                    </div>
                    {confirming ? (
                      <span className={css.confirm} role="group" aria-label={t('trajectory.diagnosis.deleteConfirm')}>
                        <button
                          type="button"
                          className={`${css.confirmButton} ${css.confirmDanger}`}
                          onClick={() => {
                            setConfirmingId(null);
                            onDelete(item.diagnosis_id);
                          }}
                          data-testid="trajectory-diagnosis-record-delete-confirm"
                        >
                          {t('trajectory.diagnosis.recordSelect.confirmDelete')}
                        </button>
                        <button
                          type="button"
                          className={css.confirmButton}
                          onClick={() => setConfirmingId(null)}
                          data-testid="trajectory-diagnosis-record-delete-cancel"
                        >
                          {t('trajectory.diagnosis.recordSelect.cancelDelete')}
                        </button>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className={css.delete}
                        disabled={running}
                        title={running
                          ? t('trajectory.diagnosis.recordSelect.deleteRunning')
                          : t('trajectory.diagnosis.delete')}
                        aria-label={running
                          ? t('trajectory.diagnosis.recordSelect.deleteRunning')
                          : t('trajectory.diagnosis.delete')}
                        onClick={() => setConfirmingId(item.diagnosis_id)}
                        data-testid="trajectory-diagnosis-record-delete"
                      >
                        <Trash2 size={14} strokeWidth={2} aria-hidden />
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      ) : null}
      </div>
    </div>
  );
});
