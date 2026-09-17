// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import type { DataValue } from '@a2ui/react';

type UnknownRecord = Record<string, unknown>;

export function isDevA2UIDiagnosticEnabled(): boolean {
  return Boolean(import.meta.env?.DEV);
}

export function a2uiDebug(message: string, data?: unknown): void {
  if (isDevA2UIDiagnosticEnabled()) {
    console.debug(message, data);
  }
}

export function a2uiWarn(message: string, data?: unknown): void {
  if (isDevA2UIDiagnosticEnabled()) {
    console.warn(message, data);
  }
}

export function a2uiError(message: string, data?: unknown): void {
  if (isDevA2UIDiagnosticEnabled()) {
    console.error(message, data);
  }
}

export function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function normalizeA2UIPath(path: unknown): string | null {
  if (typeof path !== 'string' || !path.trim()) {
    return null;
  }
  const trimmed = path.trim();
  return trimmed.startsWith('/') ? trimmed : `/${trimmed}`;
}

export function leafA2UIPath(path: string): string | null {
  const segments = path.split('/').filter(Boolean);
  if (segments.length <= 1 || !path.startsWith('/')) {
    return null;
  }
  return `/${segments[segments.length - 1]}`;
}

export function a2uiPathCandidates(path: string): string[] {
  const normalized = normalizeA2UIPath(path);
  if (!normalized) {
    return [];
  }
  const leafPath = leafA2UIPath(normalized);
  return leafPath && leafPath !== normalized ? [normalized, leafPath] : [normalized];
}

export function dualWriteA2UIValue(
  setValue: (path: string, value: DataValue) => void,
  path: string,
  value: DataValue,
): void {
  const candidates = a2uiPathCandidates(path);
  for (const candidate of candidates) {
    setValue(candidate, value);
  }
}

export function shouldFillEmptyActionValue(value: unknown): boolean {
  if (value === undefined || value === null) {
    return true;
  }
  if (Array.isArray(value)) {
    return value.length === 0;
  }
  if (isRecord(value)) {
    return Object.keys(value).length === 0;
  }
  return false;
}

export function literalArrayValues(value: unknown): unknown[] {
  if (!isRecord(value)) {
    return [];
  }
  return Array.isArray(value.literalArray)
    ? value.literalArray.filter((item) => item !== null && item !== undefined)
    : [];
}

export function selectionCollectionValues(value: unknown): unknown[] {
  if (value === null || value === undefined) {
    return [];
  }
  if (Array.isArray(value)) {
    return value.flatMap(selectionCollectionValues);
  }
  if (value instanceof Map) {
    return Array.from(value.values()).flatMap(selectionCollectionValues);
  }
  if (isRecord(value) && Object.keys(value).length === 0) {
    return [];
  }
  return [value];
}

export function optionDefaultValue(options: unknown): unknown {
  if (!Array.isArray(options)) {
    return null;
  }
  for (const option of options) {
    if (isRecord(option) && option.value !== undefined && option.value !== null) {
      return option.value;
    }
  }
  return null;
}

export function isMultiSelectChoice(props: UnknownRecord): boolean {
  const variant = typeof props.variant === 'string' ? props.variant : undefined;
  const type = typeof props.type === 'string' ? props.type : undefined;
  const maxAllowedSelections =
    typeof props.maxAllowedSelections === 'number' ? props.maxAllowedSelections : undefined;
  return variant === 'chips' || variant === 'checkbox' || type === 'chips' ||
    type === 'checkbox' || (maxAllowedSelections !== undefined && maxAllowedSelections > 1);
}

export function isSingleSelectChoice(props: UnknownRecord): boolean {
  return props.maxAllowedSelections === 1;
}

export function nextChoiceSelections(
  currentValues: string[],
  optionValue: string,
  checked: boolean,
  maxAllowedSelections?: number,
): string[] {
  if (maxAllowedSelections === 1) {
    return [optionValue];
  }

  const nextValues = [...currentValues];
  const existingIndex = nextValues.indexOf(optionValue);
  if (checked && existingIndex === -1) {
    nextValues.push(optionValue);
  } else if (!checked && existingIndex !== -1) {
    nextValues.splice(existingIndex, 1);
  }

  if (
    maxAllowedSelections !== undefined &&
    nextValues.length > maxAllowedSelections
  ) {
    return currentValues;
  }
  return nextValues;
}

export function visibleChoiceDefault(props: UnknownRecord): unknown {
  const selections = isRecord(props.selections) ? props.selections : null;
  const literalDefaults = literalArrayValues(selections);
  if (isMultiSelectChoice(props)) {
    return literalDefaults.length > 0 ? literalDefaults : null;
  }
  const optionDefault = optionDefaultValue(props.options);
  if (optionDefault !== null) {
    return [optionDefault];
  }
  return literalDefaults.length > 0 ? literalDefaults : null;
}

export function a2uiScalarText(value: unknown): string | null {
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }

  if (value instanceof Map) {
    const scalarValues = Array.from(value.values())
      .map((item) => a2uiScalarText(item))
      .filter((item): item is string => item !== null);
    return scalarValues.length === 1 ? scalarValues[0] : null;
  }

  if (Array.isArray(value)) {
    return value.length === 1 ? a2uiScalarText(value[0]) : null;
  }

  if (isRecord(value)) {
    const scalarValues = Object.values(value)
      .map((item) => a2uiScalarText(item))
      .filter((item): item is string => item !== null);
    return scalarValues.length === 1 ? scalarValues[0] : null;
  }

  return null;
}

export function resolveA2UITextValue(
  value: unknown,
  getValue: (path: string) => unknown,
): string | null {
  if (!isRecord(value)) {
    return null;
  }

  if (value.literalString !== undefined) {
    return a2uiScalarText(value.literalString);
  }

  if (value.literal !== undefined) {
    return a2uiScalarText(value.literal);
  }

  if (typeof value.path === 'string') {
    return a2uiScalarText(getValue(value.path));
  }

  return null;
}
