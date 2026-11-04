// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useId } from 'react';
import type { ChangeEvent } from 'react';
import { useTranslation } from 'react-i18next';
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

// 可见标题由提示词要求的上方 Text 组件提供；这里只给 input 一个无障碍名（不渲染可见 <label>，避免双标签）
// 走 i18n，不硬编码单一语言（检视意见 CR-9：原 dateTimeAriaLabel 写死中文，英文界面读屏为中文）
function dateTimeAriaKey(enableDate: boolean, enableTime: boolean): string {
  if (enableDate && enableTime) return 'a2ui.dateTimeAria.datetime';
  return enableTime ? 'a2ui.dateTimeAria.time' : 'a2ui.dateTimeAria.date';
}

export function DateTimeInputWithDefaults({
  node,
  surfaceId,
}: A2UIComponentProps<DateTimeInputNodeLike>) {
  const { theme, resolveString, setValue, getValue } = useA2UIComponent(node, surfaceId);
  const { t } = useTranslation();
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
        <input
          type={dateInputKind(enableDate, enableTime)}
          id={id}
          aria-label={t(dateTimeAriaKey(enableDate, enableTime))}
          value={value}
          onChange={updateFromInput}
          className={classMapToString(theme.components.DateTimeInput.element)}
          style={stylesToObject(theme.additionalStyles?.DateTimeInput)}
        />
      </section>
    </div>
  );
}
