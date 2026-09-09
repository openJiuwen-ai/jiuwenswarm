// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/** Exact OTLP attribute readers and builders for semantic validation. */

import type { OtlpAnyValue, OtlpKeyValue } from '../shared/otlp.ts'

/** An attribute lookup that preserves the original OTLP AnyValue arm. */
export type OtlpAttributeMap = ReadonlyMap<string, OtlpAnyValue>

/** Convert already duplicate-checked attributes into an exact-value map. */
export function exactAttributeMap(attributes: readonly OtlpKeyValue[] | undefined): OtlpAttributeMap {
  return new Map((attributes ?? []).map(attribute => [attribute.key, attribute.value]))
}

/** Read an exact string attribute without coercion. */
export function readStringAttribute(attributes: OtlpAttributeMap, key: string): string | undefined {
  const value = attributes.get(key)
  return value !== undefined && 'stringValue' in value ? value.stringValue : undefined
}

/** Read an exact boolean attribute without coercion. */
export function readBooleanAttribute(attributes: OtlpAttributeMap, key: string): boolean | undefined {
  const value = attributes.get(key)
  return value !== undefined && 'boolValue' in value ? value.boolValue : undefined
}

/** Read an OTLP int64 attribute as a lossless bigint. */
export function readInt64Attribute(attributes: OtlpAttributeMap, key: string): bigint | undefined {
  const value = attributes.get(key)
  if (value === undefined || !('intValue' in value)) return undefined
  try {
    return BigInt(value.intValue)
  } catch {
    return undefined
  }
}

/** Read an OTLP integer only when it can be represented exactly by JavaScript. */
export function readSafeIntegerAttribute(attributes: OtlpAttributeMap, key: string): number | undefined {
  const value = readInt64Attribute(attributes, key)
  if (value === undefined || value < BigInt(Number.MIN_SAFE_INTEGER) || value > BigInt(Number.MAX_SAFE_INTEGER)) return undefined
  return Number(value)
}

/** Read a finite numeric attribute from its double or int64 arm. */
export function readNumberAttribute(attributes: OtlpAttributeMap, key: string): number | undefined {
  const value = attributes.get(key)
  if (value === undefined) return undefined
  if ('doubleValue' in value) return value.doubleValue
  if (!('intValue' in value)) return undefined
  let integer: bigint
  try {
    integer = BigInt(value.intValue)
  } catch {
    return undefined
  }
  if (integer < BigInt(Number.MIN_SAFE_INTEGER) || integer > BigInt(Number.MAX_SAFE_INTEGER)) return undefined
  return Number(integer)
}

/** Read a homogeneous OTLP string-array attribute. */
export function readStringArrayAttribute(attributes: OtlpAttributeMap, key: string): readonly string[] | undefined {
  const value = attributes.get(key)
  if (value === undefined || !('arrayValue' in value)) return undefined
  const result: string[] = []
  for (const item of value.arrayValue.values ?? []) {
    if (!('stringValue' in item)) return undefined
    result.push(item.stringValue)
  }
  return result
}

/** Convert an OTLP AnyValue into a structured JSON-compatible value without numeric coercion. */
export function structuredOtlpValue(value: OtlpAnyValue): unknown {
  if ('stringValue' in value) return value.stringValue
  if ('boolValue' in value) return value.boolValue
  if ('intValue' in value) return value.intValue
  if ('doubleValue' in value) return value.doubleValue
  if ('bytesValue' in value) return value.bytesValue
  if ('arrayValue' in value) return (value.arrayValue.values ?? []).map(structuredOtlpValue)
  return Object.fromEntries((value.kvlistValue.values ?? []).map(item => [item.key, structuredOtlpValue(item.value)]))
}

/** Read a structured GenAI attribute, accepting the specified JSON-string fallback on spans. */
export function readStructuredAttribute(attributes: OtlpAttributeMap, key: string): unknown {
  const value = attributes.get(key)
  if (value === undefined) return undefined
  if ('stringValue' in value) {
    try {
      return JSON.parse(value.stringValue) as unknown
    } catch {
      return undefined
    }
  }
  return structuredOtlpValue(value)
}

/** Build a string OTLP AnyValue. */
export function stringValue(value: string): OtlpAnyValue {
  return { stringValue: value }
}

/** Build a boolean OTLP AnyValue. */
export function boolValue(value: boolean): OtlpAnyValue {
  return { boolValue: value }
}

/** Build an int64 OTLP AnyValue from an exact integer. */
export function intValue(value: bigint | number): OtlpAnyValue {
  return { intValue: String(value) }
}

/** Build a double OTLP AnyValue. */
export function doubleValue(value: number): OtlpAnyValue {
  return { doubleValue: value }
}

/** Build an OTLP array AnyValue. */
export function arrayValue(values: readonly OtlpAnyValue[]): OtlpAnyValue {
  return { arrayValue: { values: [...values] } }
}

/** Build an OTLP key/value-list AnyValue. */
export function kvlistValue(values: readonly OtlpKeyValue[]): OtlpAnyValue {
  return { kvlistValue: { values: [...values] } }
}

/** Build one OTLP key/value attribute. */
export function attribute(key: string, value: OtlpAnyValue): OtlpKeyValue {
  return { key, value }
}

/** Encode a JSON-compatible structured value as OTLP AnyValue. */
export function structuredValue(value: unknown): OtlpAnyValue {
  if (typeof value === 'string') return stringValue(value)
  if (typeof value === 'boolean') return boolValue(value)
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('structured OTLP numbers must be finite')
    return Number.isInteger(value) ? intValue(value) : doubleValue(value)
  }
  if (Array.isArray(value)) return arrayValue(value.map(structuredValue))
  if (typeof value === 'object' && value !== null) {
    return kvlistValue(Object.entries(value).map(([key, item]) => attribute(key, structuredValue(item))))
  }
  throw new TypeError('OTLP AnyValue cannot represent null or undefined inside this profile')
}
