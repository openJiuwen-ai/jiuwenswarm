// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import type { ServerToClientMessage } from '@a2ui/react';

export const A2UI_PROTOCOL_VERSION = '0.8';
export const A2UI_OPEN_TAG = '<a2ui-json>';
export const A2UI_CLOSE_TAG = '</a2ui-json>';

const A2UI_MESSAGE_KEYS = [
  'beginRendering',
  'surfaceUpdate',
  'dataModelUpdate',
  'deleteSurface',
] as const;

export type A2UIProtocolVersion = typeof A2UI_PROTOCOL_VERSION;

export type A2UIContentPart =
  | { kind: 'text'; text: string }
  | {
      kind: 'a2ui';
      protocolVersion: A2UIProtocolVersion;
      messages: ServerToClientMessage[];
      surfaceIds: string[];
    };

interface ParseA2UIContentOptions {
  enabled?: boolean;
  isStreaming?: boolean;
  pendingText?: string;
  invalidText?: string;
}

const DEFAULT_PENDING_TEXT = 'A2UI interface is being generated...';
const DEFAULT_INVALID_TEXT = 'The interface cannot be displayed right now. Please try again later.';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isA2UIMessage(value: unknown): value is ServerToClientMessage {
  if (!isRecord(value)) {
    return false;
  }
  return A2UI_MESSAGE_KEYS.filter((key) => key in value).length === 1;
}

function coerceA2UIMessageList(value: unknown): ServerToClientMessage[] | null {
  if (Array.isArray(value) && value.every(isA2UIMessage)) {
    return value;
  }
  if (isA2UIMessage(value)) {
    return [value];
  }
  return null;
}

function parseJsonMessageList(text: string): ServerToClientMessage[] | null {
  const trimmed = text.trim();
  if (!trimmed || (!trimmed.startsWith('[') && !trimmed.startsWith('{'))) {
    return null;
  }
  try {
    return coerceA2UIMessageList(JSON.parse(trimmed));
  } catch {
    return null;
  }
}

function parseJsonlMessages(text: string): ServerToClientMessage[] | null {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length || !lines.every((line) => line.startsWith('{') && line.endsWith('}'))) {
    return null;
  }

  const messages: ServerToClientMessage[] = [];
  try {
    for (const line of lines) {
      const parsed = JSON.parse(line);
      if (!isA2UIMessage(parsed)) {
        return null;
      }
      messages.push(parsed);
    }
  } catch {
    return null;
  }
  return messages;
}

function parseA2UIMessageText(text: string): ServerToClientMessage[] | null {
  return parseJsonlMessages(text) ?? parseJsonMessageList(text);
}

interface FencedA2UIBlock {
  start: number;
  end: number;
  messages: ServerToClientMessage[];
}

function stripLeadingOrphanedA2UIFenceTail(text: string): string {
  return text.replace(/^\s*(?:[}\]]\s*)+```[ \t]*/, '');
}

function findNextFencedA2UIBlock(content: string, cursor: number): FencedA2UIBlock | null {
  const fencePattern = /```(?:json|a2ui|a2ui-json)?[ \t]*\r?\n([\s\S]*?)\r?\n```/gi;
  fencePattern.lastIndex = cursor;

  let match: RegExpExecArray | null;
  while ((match = fencePattern.exec(content)) !== null) {
    const messages = parseA2UIMessageText(match[1]);
    if (messages) {
      return {
        start: match.index,
        end: match.index + match[0].length,
        messages,
      };
    }
  }
  return null;
}

function getMessageSurfaceId(message: ServerToClientMessage): string | null {
  if (message.beginRendering?.surfaceId) {
    return message.beginRendering.surfaceId;
  }
  if (message.surfaceUpdate?.surfaceId) {
    return message.surfaceUpdate.surfaceId;
  }
  if (message.dataModelUpdate?.surfaceId) {
    return message.dataModelUpdate.surfaceId;
  }
  if (message.deleteSurface?.surfaceId) {
    return message.deleteSurface.surfaceId;
  }
  return null;
}

export function extractA2UISurfaceIds(messages: ServerToClientMessage[]): string[] {
  const surfaceIds = new Set<string>();
  for (const message of messages) {
    const surfaceId = getMessageSurfaceId(message);
    if (surfaceId) {
      surfaceIds.add(surfaceId);
    }
  }
  return Array.from(surfaceIds);
}

function makeA2UIPart(messages: ServerToClientMessage[]): A2UIContentPart {
  return {
    kind: 'a2ui',
    protocolVersion: A2UI_PROTOCOL_VERSION,
    messages,
    surfaceIds: extractA2UISurfaceIds(messages),
  };
}

function invalidA2UIFallback(text = DEFAULT_INVALID_TEXT): A2UIContentPart {
  return {
    kind: 'text',
    text,
  };
}

/**
 * Try to extract readable text from malformed A2UI content.
 * This helps when the model generates A2UI-like content but with syntax errors.
 */
function extractTextFromMalformedA2UI(content: string): string | null {
  // Try to extract content between tags even if JSON is invalid
  const openTagIndex = content.indexOf(A2UI_OPEN_TAG);
  const closeTagIndex = content.indexOf(A2UI_CLOSE_TAG);

  if (openTagIndex >= 0 && closeTagIndex > openTagIndex) {
    const body = content.slice(
      openTagIndex + A2UI_OPEN_TAG.length,
      closeTagIndex,
    );
    // Try to find any readable text in the body
    // Look for text content in JSON-like structures
    const textMatches = body.match(/"text"\s*:\s*"([^"]+)"/g);
    if (textMatches && textMatches.length > 0) {
      const extractedTexts = textMatches
        .map((match) => {
          const textContent = match.match(/"text"\s*:\s*"([^"]+)"/);
          return textContent?.[1] ?? '';
        })
        .filter(Boolean);
      if (extractedTexts.length > 0) {
        return extractedTexts.join('\n');
      }
    }

    // Try to find any string values that look like text content
    const anyStringMatches = body.match(/"([^"]{10,})"/g);
    if (anyStringMatches && anyStringMatches.length > 0) {
      const potentialTexts = anyStringMatches
        .map((match) => match.slice(1, -1)) // Remove quotes
        .filter((text) => text.length > 10 && !text.startsWith('{') && !text.startsWith('['));
      if (potentialTexts.length > 0) {
        return potentialTexts[0]; // Return the first substantial text found
      }
    }
  }

  return null;
}

function pendingA2UIFallback(text = DEFAULT_PENDING_TEXT): A2UIContentPart {
  return {
    kind: 'text',
    text,
  };
}

export function parseA2UIContent(
  content: string,
  options: ParseA2UIContentOptions = {}
): A2UIContentPart[] {
  if (!content) {
    return [];
  }
  if (options.enabled === false) {
    return [{ kind: 'text', text: content }];
  }

  const jsonlMessages = parseJsonlMessages(content);
  if (jsonlMessages) {
    return [makeA2UIPart(jsonlMessages)];
  }

  const rawJsonMessages = parseJsonMessageList(content);
  if (rawJsonMessages) {
    return [makeA2UIPart(rawJsonMessages)];
  }

  const parts: A2UIContentPart[] = [];
  let cursor = 0;
  let sawA2UIBlock = false;

  while (cursor < content.length) {
    const openIndex = content.indexOf(A2UI_OPEN_TAG, cursor);
    const fencedBlock = findNextFencedA2UIBlock(content, cursor);
    const fencedIndex = fencedBlock?.start ?? -1;

    if (openIndex < 0 && fencedIndex < 0) {
      const tail = content.slice(cursor);
      if (tail) {
        parts.push({ kind: 'text', text: tail });
      }
      break;
    }

    const useFence = fencedBlock !== null && (openIndex < 0 || fencedBlock.start < openIndex);
    const blockStart = useFence ? fencedBlock.start : openIndex;

    sawA2UIBlock = true;
    if (blockStart > cursor) {
      const textBeforeBlock = stripLeadingOrphanedA2UIFenceTail(
        content.slice(cursor, blockStart)
      );
      if (textBeforeBlock.trim()) {
        parts.push({ kind: 'text', text: textBeforeBlock });
      }
    }

    if (useFence) {
      parts.push(makeA2UIPart(fencedBlock.messages));
      cursor = fencedBlock.end;
      continue;
    }

    const bodyStart = openIndex + A2UI_OPEN_TAG.length;
    const closeIndex = content.indexOf(A2UI_CLOSE_TAG, bodyStart);
    if (closeIndex < 0) {
      parts.push(
        options.isStreaming
          ? pendingA2UIFallback(options.pendingText)
          : invalidA2UIFallback(options.invalidText)
      );
      break;
    }

    const body = content.slice(bodyStart, closeIndex);
    const messages = parseJsonMessageList(body);
    if (messages) {
      parts.push(makeA2UIPart(messages));
    } else {
      // Try to extract readable text from malformed A2UI content
      const extractedText = extractTextFromMalformedA2UI(content.slice(openIndex, closeIndex + A2UI_CLOSE_TAG.length));
      if (extractedText) {
        parts.push({ kind: 'text', text: extractedText });
      } else {
        parts.push(invalidA2UIFallback(options.invalidText));
      }
    }
    cursor = closeIndex + A2UI_CLOSE_TAG.length;
  }

  return sawA2UIBlock ? parts.filter((part) => part.kind !== 'text' || part.text) : [{ kind: 'text', text: content }];
}

export function namespaceA2UIMessages(
  messages: ServerToClientMessage[],
  namespace: string
): ServerToClientMessage[] {
  const namespaceSurfaceId = (surfaceId: string): string => (
    /^msg_[A-Za-z0-9_-]+:/.test(surfaceId) ? surfaceId : `${namespace}:${surfaceId}`
  );

  return messages.map((message) => {
    const cloned = structuredClone(message) as ServerToClientMessage;
    if (cloned.beginRendering?.surfaceId) {
      cloned.beginRendering.surfaceId = namespaceSurfaceId(cloned.beginRendering.surfaceId);
    }
    if (cloned.surfaceUpdate?.surfaceId) {
      cloned.surfaceUpdate.surfaceId = namespaceSurfaceId(cloned.surfaceUpdate.surfaceId);
    }
    if (cloned.dataModelUpdate?.surfaceId) {
      cloned.dataModelUpdate.surfaceId = namespaceSurfaceId(cloned.dataModelUpdate.surfaceId);
    }
    if (cloned.deleteSurface?.surfaceId) {
      cloned.deleteSurface.surfaceId = namespaceSurfaceId(cloned.deleteSurface.surfaceId);
    }
    return cloned;
  });
}

export function a2uiContentToText(content: string): string {
  return parseA2UIContent(content)
    .map((part) => {
      if (part.kind === 'text') {
        return part.text.trim();
      }
      return '[A2UI interactive content]';
    })
    .filter(Boolean)
    .join('\n\n');
}

/**
 * 检查内容是否为 A2UI client event（内部交互事件，不应显示）。
 * 只判断 content，不判断 role —— 调用处应明确只过滤 user role。
 * 支持: object / string JSON / array / JSON-ish fallback
 */
export function isA2UIClientEventContent(content: unknown): boolean {
  if (content == null) return false;

  if (Array.isArray(content)) {
    return content.some(isA2UIClientEventContent);
  }

  if (typeof content === 'object') {
    const record = content as Record<string, unknown>;
    if (record.type === 'a2ui.client_event') return true;
    if (isA2UIClientEventContent(record.content)) return true;
    if (isA2UIClientEventContent(record.event)) return true;
    if (isA2UIClientEventContent(record.data)) return true;
    return false;
  }

  if (typeof content !== 'string') return false;

  const trimmed = content.trim();
  if (!trimmed) return false;

  try {
    const parsed = JSON.parse(trimmed);
    return isA2UIClientEventContent(parsed);
  } catch {
    const looksStructured = trimmed.startsWith('{') || trimmed.startsWith('[');
    if (!looksStructured) return false;

    return (
      trimmed.includes('"type"') && trimmed.includes('a2ui.client_event')
    ) || (
      trimmed.includes("'type'") && trimmed.includes('a2ui.client_event')
    );
  }
}
