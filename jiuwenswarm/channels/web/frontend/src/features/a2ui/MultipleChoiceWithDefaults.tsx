// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useId, useMemo, useState } from 'react';
import type { CSSProperties, ChangeEvent } from 'react';
import {
  classMapToString,
  stylesToObject,
  useA2UIComponent,
  type A2UIComponentProps,
  type AnyComponentNode,
  type DataValue,
} from '@a2ui/react';
import {
  a2uiError,
  a2uiWarn,
  dualWriteA2UIValue,
  isMultiSelectChoice,
  isSingleSelectChoice,
  literalArrayValues,
  nextChoiceSelections,
  selectionCollectionValues,
  visibleChoiceDefault as visibleChoiceDefaultValue,
} from './formDefaults';

type MultipleChoiceNodeLike = Extract<AnyComponentNode, { type: 'MultipleChoice' }>;

function choiceValueToString(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function nonEmptyChoiceValue(value: unknown): string | null {
  const text = choiceValueToString(value);
  return text !== '' ? text : null;
}

function selectedValuesFromData(value: DataValue | null): string[] {
  return selectionCollectionValues(value)
    .map(nonEmptyChoiceValue)
    .filter((item): item is string => item !== null);
}

export function visibleMultipleChoiceDefault(
  props: MultipleChoiceNodeLike['properties']
): string | null {
  const defaultValue = visibleChoiceDefaultValue(props as unknown as Record<string, unknown>);
  if (Array.isArray(defaultValue)) {
    const firstDefault = defaultValue
      .map(nonEmptyChoiceValue)
      .find((item): item is string => item !== null);
    return firstDefault ?? null;
  }
  return nonEmptyChoiceValue(defaultValue);
}

export function MultipleChoiceWithDefaults({
  node,
  surfaceId,
}: A2UIComponentProps<MultipleChoiceNodeLike>) {
  const { theme, resolveString, setValue, getValue } = useA2UIComponent(node, surfaceId);
  const props = node.properties;
  const id = useId();
  const selectionsPath = props.selections?.path;

  // Read label from schema: try 'label' first, then 'description', then fallback
  const rawLabel = (props as unknown as Record<string, unknown>).label;
  const rawDescription = (props as unknown as Record<string, unknown>).description;
  const descriptionValue = rawLabel ?? rawDescription;
  const description = resolveString(descriptionValue as Parameters<typeof resolveString>[0]) ?? 'Select an item';

  const rawProps = props as unknown as Record<string, unknown>;
  const isMultiSelect = isMultiSelectChoice(rawProps);
  const literalDefaults = literalArrayValues(props.selections)
    .map(nonEmptyChoiceValue)
    .filter((item): item is string => item !== null);
  const defaultValue = visibleMultipleChoiceDefault(props);
  const dataValue = selectionsPath ? getValue(selectionsPath) : null;
  const selectedValues = selectedValuesFromData(dataValue);
  const effectiveSelectedValues = selectedValues.length > 0 ? selectedValues : literalDefaults;
  const maxAllowedSelections = rawProps.maxAllowedSelections as number | undefined;
  // @a2ui/web_core schema defines 'variant' but TS types may not include it
  const variant = rawProps.variant as string | undefined;
  const type = variant ?? rawProps.type as string | undefined;

  const isSingleSelect = isSingleSelectChoice(rawProps);

  // Filterable search support
  const filterable = rawProps.filterable as boolean | undefined;
  const options = props.options ?? [];
  const shouldShowFilter = filterable === true || options.length >= 10;
  const [searchQuery, setSearchQuery] = useState('');

  const filteredOptions = useMemo(() => {
    if (!shouldShowFilter || !searchQuery.trim()) return options;
    const q = searchQuery.trim().toLowerCase();
    return options.filter((option) => {
      const label = resolveString(option.label) ?? '';
      const value = choiceValueToString(option.value);
      return (
        label.toLowerCase().includes(q) ||
        value.toLowerCase().includes(q)
      );
    });
  }, [options, searchQuery, shouldShowFilter, resolveString]);

  const handleSingleChange = useCallback(
    (event: ChangeEvent<HTMLSelectElement>) => {
      if (selectionsPath) {
        dualWriteA2UIValue(setValue, selectionsPath, [event.target.value]);
      }
    },
    [selectionsPath, setValue],
  );

  const handleMultiChange = useCallback(
    (optionValue: string, checked: boolean) => {
      if (!selectionsPath) {
        a2uiWarn('[A2UI-MC] selectionsPath is undefined, cannot write');
        return;
      }

      const nextValues = nextChoiceSelections(
        effectiveSelectedValues,
        optionValue,
        checked,
        maxAllowedSelections,
      );
      dualWriteA2UIValue(setValue, selectionsPath, nextValues);
    },
    [selectionsPath, effectiveSelectedValues, maxAllowedSelections, setValue],
  );

  const hostStyle = (
    node.weight !== undefined ? { '--weight': node.weight } : {}
  ) as CSSProperties;

  // Build label text with multi-select hint
  const labelText = useMemo(() => {
    if (isSingleSelect || maxAllowedSelections === undefined) return description;
    return `${description}（最多选 ${maxAllowedSelections} 项）`;
  }, [description, isSingleSelect, maxAllowedSelections]);

  // Render chips UI
  if (type === 'chips') {
    return (
      <div className="a2ui-multiplechoice" style={hostStyle}>
        <section className={classMapToString(theme.components.MultipleChoice.container)}>
          <label
            className={classMapToString(theme.components.MultipleChoice.label)}
          >
            {labelText}
          </label>
          {shouldShowFilter && (
            <input
              type="search"
              placeholder="搜索选项"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="mb-2 px-3 py-1 border border-gray-300 rounded text-sm w-full"
            />
          )}
          <div className="flex flex-wrap gap-2">
            {filteredOptions.map((option, index) => {
              const optionValue = choiceValueToString(option.value);
              const optionLabel = resolveString(option.label) ?? optionValue;
              const isSelected = effectiveSelectedValues.includes(optionValue);
              return (
                <button
                  key={optionValue || `option-${index}`}
                  type="button"
                  className={`px-3 py-1 rounded-full border ${
                    isSelected
                      ? 'bg-blue-500 text-white border-blue-500'
                      : 'bg-white text-gray-700 border-gray-300 hover:border-blue-300'
                  }`}
                  aria-pressed={isSelected}
                  onClick={() => {
                    try {
                      handleMultiChange(optionValue, !isSelected);
                    } catch (err) {
                      a2uiError('[A2UI-MC] handleMultiChange threw:', err);
                    }
                  }}
                >
                  {optionLabel}
                </button>
              );
            })}
          </div>
        </section>
      </div>
    );
  }

  // Render checkboxes UI
  if (isMultiSelect) {
    return (
      <div className="a2ui-multiplechoice" style={hostStyle}>
        <section className={classMapToString(theme.components.MultipleChoice.container)}>
          <label
            className={classMapToString(theme.components.MultipleChoice.label)}
          >
            {labelText}
          </label>
          {shouldShowFilter && (
            <input
              type="search"
              placeholder="搜索选项"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="mb-2 px-3 py-1 border border-gray-300 rounded text-sm w-full"
            />
          )}
          <div className="flex flex-col gap-1">
            {filteredOptions.map((option, index) => {
              const optionValue = choiceValueToString(option.value);
              const optionLabel = resolveString(option.label) ?? optionValue;
              const isSelected = effectiveSelectedValues.includes(optionValue);
              return (
                <label
                  key={optionValue || `option-${index}`}
                  className="flex items-center gap-2 cursor-pointer"
                >
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={(e) => handleMultiChange(optionValue, e.target.checked)}
                    className="w-4 h-4"
                  />
                  <span>{optionLabel}</span>
                </label>
              );
            })}
          </div>
        </section>
      </div>
    );
  }

  // Render single-select dropdown
  const selectedValue = selectedValues[0] ?? defaultValue ?? '';
  return (
    <div className="a2ui-multiplechoice" style={hostStyle}>
      <section className={classMapToString(theme.components.MultipleChoice.container)}>
        <label
          htmlFor={id}
          className={classMapToString(theme.components.MultipleChoice.label)}
        >
          {labelText}
        </label>
        <select
          name="data"
          id={id}
          value={selectedValue}
          className={classMapToString(theme.components.MultipleChoice.element)}
          style={stylesToObject(theme.additionalStyles?.MultipleChoice)}
          onChange={handleSingleChange}
        >
          {(props.options ?? []).map((option, index) => {
            const optionValue = choiceValueToString(option.value);
            const optionLabel = resolveString(option.label) ?? optionValue;
            return (
              <option key={optionValue || `option-${index}`} value={optionValue}>
                {optionLabel}
              </option>
            );
          })}
        </select>
      </section>
    </div>
  );
}
