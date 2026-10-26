// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useMemo } from 'react';
import type { CSSProperties } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  classMapToString,
  stylesToObject,
  useA2UIComponent,
  type A2UIComponentProps,
  type AnyComponentNode,
} from '@a2ui/react';
import { hostWeightStyle } from './fieldBinding';
import { isRecord, resolveA2UITextValue } from './formDefaults';

type TextNodeLike = Extract<AnyComponentNode, { type: 'Text' }>;
type TextUsageHint = 'h1' | 'h2' | 'h3' | 'h4' | 'h5' | 'caption' | 'body';
type ClassMap = Record<string, boolean>;

function textClassMap(value: unknown): ClassMap {
  return isRecord(value) ? value as ClassMap : {};
}

export function TextWithDefaults({
  node,
  surfaceId,
}: A2UIComponentProps<TextNodeLike>) {
  const { theme, getValue } = useA2UIComponent(node, surfaceId);
  const props = node.properties;
  const usageHint = props.usageHint as TextUsageHint | undefined;
  const textValue = resolveA2UITextValue(props.text, getValue);

  const classes = useMemo(() => {
    const textTheme = theme.components.Text as Record<string, unknown>;
    return classMapToString({
      ...textClassMap(textTheme.all),
      ...textClassMap(usageHint ? textTheme[usageHint] : undefined),
    });
  }, [theme.components.Text, usageHint]);

  const additionalStyles = useMemo(() => {
    const textStyles = theme.additionalStyles?.Text;
    if (!textStyles) return undefined;
    if (isRecord(textStyles) && usageHint && isRecord(textStyles[usageHint])) {
      return stylesToObject(textStyles[usageHint] as Record<string, string>);
    }
    return stylesToObject(textStyles as Record<string, string>);
  }, [theme.additionalStyles?.Text, usageHint]);

  if (textValue === null || textValue === '') {
    return null;
  }

  const textStyle = usageHint === 'caption'
    ? { ...additionalStyles, fontStyle: 'italic' } as CSSProperties
    : additionalStyles;

  return (
    <div className="a2ui-text" style={hostWeightStyle(node.weight)}>
      <section className={classes} style={textStyle}>
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {textValue}
        </ReactMarkdown>
      </section>
    </div>
  );
}
