// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useCallback, useId, useState } from 'react';
import type { ChangeEvent, HTMLInputTypeAttribute } from 'react';
import {
  classMapToString,
  stylesToObject,
  useA2UIComponent,
  type A2UIComponentProps,
  type AnyComponentNode,
} from '@a2ui/react';
import { hostWeightStyle, useA2UIBoundValue } from './fieldBinding';

type TextFieldNodeLike = Extract<AnyComponentNode, { type: 'TextField' }>;
type TextControlKind = 'shortText' | 'longText' | 'number' | 'date' | 'obscured';

function textControlKind(props: TextFieldNodeLike['properties']): TextControlKind {
  const extraProps = props as unknown as Record<string, unknown>;
  return String(extraProps.textFieldType ?? extraProps.type ?? 'shortText') as TextControlKind;
}

function htmlInputType(kind: TextControlKind): HTMLInputTypeAttribute {
  if (kind === 'obscured') return 'password';
  if (kind === 'number' || kind === 'date') return kind;
  return 'text';
}

export function TextFieldWithDefaults({
  node,
  surfaceId,
}: A2UIComponentProps<TextFieldNodeLike>) {
  const { theme, resolveString, setValue, getValue } = useA2UIComponent(node, surfaceId);
  const props = node.properties;
  const id = useId();
  const label = resolveString(props.label);
  const fieldKind = textControlKind(props);
  const validationRegexp = props.validationRegexp;
  const stringifyValue = useCallback((value: unknown) => String(value), []);
  const hasVisibleDefault = useCallback((value: string) => value !== '', []);
  const [value, commitValue] = useA2UIBoundValue({
    path: props.text?.path,
    initialValue: resolveString(props.text) ?? '',
    getValue,
    setValue,
    fromModelValue: stringifyValue,
    shouldSeedInitial: hasVisibleDefault,
  });
  const [_isValid, setIsValid] = useState(true);

  const updateFromInput = useCallback(
    (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
      const newValue = event.target.value;
      commitValue(newValue);
      if (validationRegexp) {
        setIsValid(new RegExp(validationRegexp).test(newValue));
      }
    },
    [commitValue, validationRegexp],
  );

  return (
    <div className="a2ui-textfield" style={hostWeightStyle(node.weight)}>
      <section className={classMapToString(theme.components.TextField.container)}>
        {label && (
          <label
            htmlFor={id}
            className={classMapToString(theme.components.TextField.label)}
          >
            {label}
          </label>
        )}
        {fieldKind === 'longText' ? (
          <textarea
            id={id}
            value={value}
            onChange={updateFromInput}
            placeholder="Please enter a value"
            className={classMapToString(theme.components.TextField.element)}
            style={stylesToObject(theme.additionalStyles?.TextField)}
          />
        ) : (
          <input
            type={htmlInputType(fieldKind)}
            id={id}
            value={value}
            onChange={updateFromInput}
            placeholder="Please enter a value"
            className={classMapToString(theme.components.TextField.element)}
            style={stylesToObject(theme.additionalStyles?.TextField)}
          />
        )}
      </section>
    </div>
  );
}
