// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useId } from 'react';
import type { ChangeEvent } from 'react';
import {
  classMapToString,
  stylesToObject,
  useA2UIComponent,
  type A2UIComponentProps,
  type AnyComponentNode,
} from '@a2ui/react';
import { hostWeightStyle, useA2UIBoundValue } from './fieldBinding';

type DateTimeInputNodeLike = Extract<AnyComponentNode, { type: 'DateTimeInput' }>;

type DateInputKind = 'date' | 'time' | 'datetime-local';

function dateInputKind(enableDate: boolean, enableTime: boolean): DateInputKind {
  if (enableDate && enableTime) return 'datetime-local';
  return enableTime ? 'time' : 'date';
}

function dateInputLabel(enableDate: boolean, enableTime: boolean): string {
  if (enableDate && enableTime) return 'Date & Time';
  return enableTime ? 'Time' : 'Date';
}

export function DateTimeInputWithDefaults({
  node,
  surfaceId,
}: A2UIComponentProps<DateTimeInputNodeLike>) {
  const { theme, resolveString, setValue, getValue } = useA2UIComponent(node, surfaceId);
  const props = node.properties;
  const id = useId();
  const enableDate = props.enableDate ?? true;
  const enableTime = props.enableTime ?? false;
  const stringifyValue = useCallback((value: unknown) => String(value), []);
  const hasVisibleDefault = useCallback((value: string) => value !== '', []);
  const [value, commitValue] = useA2UIBoundValue({
    path: props.value?.path,
    initialValue: resolveString(props.value) ?? '',
    getValue,
    setValue,
    fromModelValue: stringifyValue,
    shouldSeedInitial: hasVisibleDefault,
  });

  const updateFromInput = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      commitValue(event.target.value);
    },
    [commitValue],
  );

  return (
    <div className="a2ui-datetime-input" style={hostWeightStyle(node.weight)}>
      <section className={classMapToString(theme.components.DateTimeInput.container)}>
        <label
          htmlFor={id}
          className={classMapToString(theme.components.DateTimeInput.label)}
        >
          {dateInputLabel(enableDate, enableTime)}
        </label>
        <input
          type={dateInputKind(enableDate, enableTime)}
          id={id}
          value={value}
          onChange={updateFromInput}
          className={classMapToString(theme.components.DateTimeInput.element)}
          style={stylesToObject(theme.additionalStyles?.DateTimeInput)}
        />
      </section>
    </div>
  );
}
