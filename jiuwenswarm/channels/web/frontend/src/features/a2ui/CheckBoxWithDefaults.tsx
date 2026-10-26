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

type CheckBoxNodeLike = Extract<AnyComponentNode, { type: 'CheckBox' }>;

export function CheckBoxWithDefaults({
  node,
  surfaceId,
}: A2UIComponentProps<CheckBoxNodeLike>) {
  const { theme, resolveString, resolveBoolean, setValue, getValue } = useA2UIComponent(
    node,
    surfaceId,
  );
  const props = node.properties;
  const id = useId();
  const label = resolveString(props.label);
  const readBoolean = useCallback((value: unknown) => Boolean(value), []);
  const [checked, commitChecked] = useA2UIBoundValue({
    path: props.value?.path,
    initialValue: resolveBoolean(props.value) ?? false,
    literalValue: props.value?.literalBoolean,
    getValue,
    setValue,
    fromModelValue: readBoolean,
  });

  const updateFromInput = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      commitChecked(event.target.checked);
    },
    [commitChecked],
  );

  return (
    <div className="a2ui-checkbox" style={hostWeightStyle(node.weight)}>
      <section
        className={classMapToString(theme.components.CheckBox.container)}
        style={stylesToObject(theme.additionalStyles?.CheckBox)}
      >
        <input
          type="checkbox"
          id={id}
          checked={checked}
          onChange={updateFromInput}
          className={classMapToString(theme.components.CheckBox.element)}
        />
        {label && (
          <label
            htmlFor={id}
            className={classMapToString(theme.components.CheckBox.label)}
          >
            {label}
          </label>
        )}
      </section>
    </div>
  );
}
