// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import type { A2UIClientEventMessage } from '@a2ui/react';
import { isA2UIFeatureEnabled } from './featureConfig';
import { A2UI_PROTOCOL_VERSION } from './a2uiContent';
import { enrichA2UIClientEventWithDefaults } from './actionDefaults';
import { a2uiDebug, a2uiWarn } from './formDefaults';

export interface A2UIClientEventContent {
  type: 'a2ui.client_event';
  protocolVersion: typeof A2UI_PROTOCOL_VERSION;
  event: A2UIClientEventMessage;
}

type A2UIActionHandler = (
  message: A2UIClientEventMessage
) => void | Promise<void>;

let currentHandler: A2UIActionHandler | null = null;
const inFlightActionKeys = new Set<string>();

function inFlightActionKey(message: A2UIClientEventMessage): string | null {
  const userAction = message.userAction;
  if (!userAction) {
    return null;
  }
  const surfaceId = userAction.surfaceId || '';
  const sourceComponentId = userAction.sourceComponentId || '';
  const actionName = userAction.name || '';
  if (!surfaceId || !sourceComponentId || !actionName) {
    return null;
  }
  return `${surfaceId}\u0000${sourceComponentId}\u0000${actionName}`;
}

/**
 * Clean up action context to fix model-generated key issues.
 * The model sometimes generates object keys instead of strings,
 * which JS coerces to "[object Object]". This function:
 * 1. Filters out [object Object] keys
 * 2. Attempts to recover the correct key from path-like objects
 */
function cleanActionContext(
  context: Record<string, unknown> | undefined
): Record<string, unknown> | undefined {
  if (!context) return context;

  const cleaned: Record<string, unknown> = {};
  for (const [rawKey, rawValue] of Object.entries(context)) {
    if (rawKey === '[object Object]') {
      // Try to recover: if value has a path property, extract the last segment
      if (rawValue && typeof rawValue === 'object' && 'path' in rawValue) {
        const path = (rawValue as { path: string }).path;
        if (typeof path === 'string') {
          const segments = path.split('/').filter(Boolean);
          if (segments.length > 0 && segments[segments.length - 1] !== '') {
            const recoveredKey = segments[segments.length - 1];
            // Validate: recovered key must look like a field name (alphanumeric + underscore)
            if (/^[a-zA-Z_][a-zA-Z0-9_]*$/.test(recoveredKey)) {
              a2uiDebug('[A2UI] recovered context key from [object Object]:', {
                recoveredKey,
                originalPath: path,
              });
              cleaned[recoveredKey] = rawValue;
              continue;
            }
            a2uiWarn('[A2UI] dropped invalid context entry:', {
              rawKey,
              rawValue: typeof rawValue === 'object' ? JSON.stringify(rawValue)?.substring(0, 100) : rawValue,
              normalizedKey: recoveredKey,
              dropReason: 'recovered key failed field name validation',
            });
            continue;
          }
        }
      }
      // Cannot recover — drop the entry entirely
      a2uiWarn('[A2UI] dropped invalid context entry:', {
        rawKey,
        rawValue: typeof rawValue === 'string' ? rawValue.substring(0, 50) : typeof rawValue,
        normalizedKey: null,
        dropReason: 'unrecoverable [object Object] key',
      });
      continue;
    }
    // Also skip empty string keys
    if (rawKey === '') {
      a2uiWarn('[A2UI] dropped invalid context entry:', {
        rawKey: '(empty)',
        rawValue: typeof rawValue === 'string' ? rawValue.substring(0, 50) : typeof rawValue,
        normalizedKey: null,
        dropReason: 'empty string key',
      });
      continue;
    }
    cleaned[rawKey] = rawValue;
  }
  return Object.keys(cleaned).length > 0 ? cleaned : undefined;
}

export function buildA2UIClientEventContent(
  message: A2UIClientEventMessage
): A2UIClientEventContent {
  const enrichedMessage = enrichA2UIClientEventWithDefaults(message);
  return {
    type: 'a2ui.client_event',
    protocolVersion: A2UI_PROTOCOL_VERSION,
    event: enrichedMessage,
  };
}

export function setA2UIActionHandler(
  handler: A2UIActionHandler | null
): () => void {
  currentHandler = handler;
  inFlightActionKeys.clear();
  return () => {
    if (currentHandler === handler) {
      currentHandler = null;
      inFlightActionKeys.clear();
    }
  };
}

export async function dispatchA2UIAction(
  message: A2UIClientEventMessage
): Promise<void> {
  if (!isA2UIFeatureEnabled()) {
    return;
  }
  if (!currentHandler) {
    a2uiWarn('[A2UI] action ignored because no chat sender is registered');
    return;
  }

  // Clean up context to fix [object Object] key issues
  if (message.userAction?.context) {
    const cleaned = cleanActionContext(message.userAction.context);
    if (cleaned !== message.userAction.context) {
      message = {
        ...message,
        userAction: {
          ...message.userAction,
          context: cleaned,
        },
      };
    }
  }

  // Log the action for debugging
  const userAction = message.userAction;
  if (userAction) {
    a2uiDebug('[A2UI] dispatching action:', {
      name: userAction.name,
      sourceComponentId: userAction.sourceComponentId,
      surfaceId: userAction.surfaceId,
      contextKeys: userAction.context ? Object.keys(userAction.context) : [],
      contextValues: userAction.context,
      contextEntries: userAction.context ? Object.entries(userAction.context).map(([k, v]) => ({
        key: k,
        keyType: typeof k,
        valueType: typeof v,
        value: v === null ? 'null' : v === undefined ? 'undefined' : JSON.stringify(v)?.substring(0, 100),
      })) : [],
    });
  }

  const actionKey = inFlightActionKey(message);
  if (actionKey && inFlightActionKeys.has(actionKey)) {
    a2uiWarn('[A2UI] duplicate action ignored while request is in flight:', {
      name: userAction?.name,
      sourceComponentId: userAction?.sourceComponentId,
      surfaceId: userAction?.surfaceId,
    });
    return;
  }

  if (actionKey) {
    inFlightActionKeys.add(actionKey);
  }
  try {
    await currentHandler(message);
  } finally {
    if (actionKey) {
      inFlightActionKeys.delete(actionKey);
    }
  }
}
