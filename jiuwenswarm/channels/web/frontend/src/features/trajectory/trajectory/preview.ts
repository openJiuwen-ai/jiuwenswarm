// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Bounded Markdown-to-text projection shared by trajectory consumers.
 * Adapted mechanically from `packages/client/ui-trajectory/src/client/trajectory-preview.ts`
 * under the repository MIT license.
 */

import { extractMarkdownPlainText } from '../primitives/markdown/plain-text.ts'

const PREVIEW_SOURCE_CHARACTERS = 2_048
const PREVIEW_OUTPUT_CHARACTERS = 512

/**
 * Build a bounded one-line preview without parsing the complete Markdown document.
 * @param text - Untrusted message, reasoning, payload, or result text.
 * @returns A compact preview capped independently from the retained source.
 */
export function trajectoryPreviewText(text: string): string {
  const source = text.slice(0, PREVIEW_SOURCE_CHARACTERS)
  const compact = extractMarkdownPlainText(source).replace(/\s+/g, ' ').trim()
  const preview = compact.slice(0, PREVIEW_OUTPUT_CHARACTERS).trimEnd()
  return source.length < text.length || preview.length < compact.length
    ? `${preview}…`
    : preview
}

/**
 * Combine a record label with its Markdown preview without echoing the same
 * assistant output twice when the label is the retained source text.
 */
export function trajectoryDisplayText(text: string, markdown: string): string {
  const preview = trajectoryPreviewText(markdown)
  if (text === '') return preview
  if (preview === '') return text
  return trajectoryPreviewText(text) === preview ? text : `${text} · ${preview}`
}
