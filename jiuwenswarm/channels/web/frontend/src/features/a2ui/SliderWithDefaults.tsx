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

type SliderNodeLike = Extract<AnyComponentNode, { type: 'Slider' }>;

function numberFromModel(value: unknown): number {
  return Number(value);
}

export function SliderWithDefaults({
  node,
  surfaceId,
}: A2UIComponentProps<SliderNodeLike>) {
  const { theme, resolveNumber, resolveString, setValue, getValue } = useA2UIComponent(
    node,
    surfaceId,
  );
  const props = node.properties;
  const id = useId();
  const minValue = props.minValue ?? 0;
  const maxValue = props.maxValue ?? 0;
  const [value, commitValue] = useA2UIBoundValue({
    path: props.value?.path,
    initialValue: resolveNumber(props.value) ?? 0,
    literalValue: props.value?.literalNumber,
    getValue,
    setValue,
    fromModelValue: numberFromModel,
  });

  const updateFromInput = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      commitValue(Number(event.target.value));
    },
    [commitValue],
  );

  const labelValue = (props as unknown as Record<string, unknown>).label;
  const label = labelValue ? resolveString(labelValue as Parameters<typeof resolveString>[0]) : '';

  return (
    <div className="a2ui-slider" style={hostWeightStyle(node.weight)}>
      <section className={classMapToString(theme.components.Slider.container)}>
        <label
          htmlFor={id}
          className={classMapToString(theme.components.Slider.label)}
        >
          {label}
        </label>
        <input
          type="range"
          id={id}
          name="data"
          value={value}
          min={minValue}
          max={maxValue}
          onChange={updateFromInput}
          className={classMapToString(theme.components.Slider.element)}
          style={stylesToObject(theme.additionalStyles?.Slider)}
        />
        <span className={classMapToString(theme.components.Slider.label)}>
          {value}
        </span>
      </section>
    </div>
  );
}
