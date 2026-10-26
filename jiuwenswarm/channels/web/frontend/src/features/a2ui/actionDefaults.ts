// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import type { A2UIClientEventMessage, ServerToClientMessage } from '@a2ui/react';
import {
  a2uiDebug,
  a2uiPathCandidates,
  isRecord,
  normalizeA2UIPath,
  shouldFillEmptyActionValue,
  visibleChoiceDefault,
} from './formDefaults';

type UnknownRecord = Record<string, unknown>;

interface ActionDefaultEntry {
  surfaceId: string;
  sourceComponentId: string;
  actionName: string;
  defaults: Record<string, unknown>;
}

const actionDefaults = new Map<string, ActionDefaultEntry>();
const surfaceDefaultsByPath = new Map<string, Map<string, unknown>>();

function componentType(component: unknown): string | null {
  if (!isRecord(component)) {
    return null;
  }
  return Object.keys(component)[0] ?? null;
}

function componentProps(component: unknown): UnknownRecord | null {
  if (!isRecord(component)) {
    return null;
  }
  const type = componentType(component);
  if (!type || !isRecord(component[type])) {
    return null;
  }
  return component[type];
}

function actionName(action: UnknownRecord): string | null {
  return typeof action.name === 'string'
    ? action.name
    : typeof action.actionName === 'string'
      ? action.actionName
      : null;
}

function actionKey(surfaceId: string, sourceComponentId: string, name: string): string {
  return `${surfaceId}\u0000${sourceComponentId}\u0000${name}`;
}

function rememberDefault(
  defaultsByPath: Map<string, unknown>,
  path: string | null,
  value: unknown,
): void {
  if (!path || value === null || value === undefined) {
    return;
  }
  for (const candidate of a2uiPathCandidates(path)) {
    defaultsByPath.set(candidate, value);
  }
}

function recordComponentDefault(
  defaultsByPath: Map<string, unknown>,
  component: UnknownRecord,
): void {
  const type = componentType(component);
  const props = componentProps(component);
  if (!type || !props) {
    return;
  }

  if (type === 'MultipleChoice' || type === 'SingleChoice') {
    const selections = isRecord(props.selections) ? props.selections : null;
    const selectionsPath = normalizeA2UIPath(selections?.path);
    rememberDefault(defaultsByPath, selectionsPath, visibleChoiceDefault(props));
    return;
  }

  if (type === 'TextField') {
    const text = isRecord(props.text) ? props.text : null;
    rememberDefault(defaultsByPath, normalizeA2UIPath(text?.path), text?.literalString);
    return;
  }

  if (type === 'Slider') {
    const value = isRecord(props.value) ? props.value : null;
    rememberDefault(defaultsByPath, normalizeA2UIPath(value?.path), value?.literalNumber);
    return;
  }

  if (type === 'CheckBox') {
    const value = isRecord(props.value) ? props.value : null;
    rememberDefault(defaultsByPath, normalizeA2UIPath(value?.path), value?.literalBoolean);
    return;
  }

  if (type === 'DateTimeInput') {
    const value = isRecord(props.value) ? props.value : null;
    rememberDefault(defaultsByPath, normalizeA2UIPath(value?.path), value?.literalString);
  }
}

export function clearA2UIActionDefaults(): void {
  actionDefaults.clear();
  surfaceDefaultsByPath.clear();
}

export function recordA2UIActionDefaults(messages: ServerToClientMessage[]): void {
  for (const message of messages) {
    if (message.deleteSurface?.surfaceId) {
      const surfaceId = message.deleteSurface.surfaceId;
      surfaceDefaultsByPath.delete(surfaceId);
      for (const [key, entry] of actionDefaults.entries()) {
        if (entry.surfaceId === surfaceId) {
          actionDefaults.delete(key);
        }
      }
      continue;
    }

    const update = message.surfaceUpdate;
    if (!update?.surfaceId || !Array.isArray(update.components)) {
      continue;
    }

    const defaultsByPath = surfaceDefaultsByPath.get(update.surfaceId) ?? new Map<string, unknown>();
    for (const instance of update.components) {
      if (!isRecord(instance.component)) {
        continue;
      }
      recordComponentDefault(defaultsByPath, instance.component);
    }
    if (defaultsByPath.size > 0) {
      surfaceDefaultsByPath.set(update.surfaceId, defaultsByPath);
    }

    for (const instance of update.components) {
      const props = componentProps(instance.component);
      if (!props || componentType(instance.component) !== 'Button' || !isRecord(props.action)) {
        continue;
      }

      const name = actionName(props.action);
      if (!name || !Array.isArray(props.action.context)) {
        continue;
      }

      const defaults: Record<string, unknown> = {};
      for (const contextItem of props.action.context) {
        if (!isRecord(contextItem) || typeof contextItem.key !== 'string' || !isRecord(contextItem.value)) {
          continue;
        }
        const path = normalizeA2UIPath(contextItem.value.path);
        if (path && defaultsByPath.has(path)) {
          defaults[contextItem.key] = defaultsByPath.get(path);
        }
      }

      if (Object.keys(defaults).length > 0) {
        a2uiDebug('[A2UI-AD] recording action defaults:', {
          surfaceId: update.surfaceId,
          componentId: instance.id,
          actionName: name,
          defaults,
        });
        actionDefaults.set(actionKey(update.surfaceId, instance.id, name), {
          surfaceId: update.surfaceId,
          sourceComponentId: instance.id,
          actionName: name,
          defaults,
        });
      }
    }
  }
}

export function enrichA2UIClientEventWithDefaults(
  message: A2UIClientEventMessage
): A2UIClientEventMessage {
  const userAction = message.userAction;
  if (!userAction) {
    return message;
  }

  const name = actionName(userAction as unknown as UnknownRecord);
  const sourceComponentId = userAction.sourceComponentId;
  const surfaceId = userAction.surfaceId;
  if (!name || !sourceComponentId || !surfaceId) {
    return message;
  }

  const entry = actionDefaults.get(actionKey(surfaceId, sourceComponentId, name));
  if (!entry) {
    return message;
  }

  const currentContext = isRecord(userAction.context) ? userAction.context : {};
  const nextContext: Record<string, unknown> = { ...currentContext };
  let changed = false;
  for (const [key, value] of Object.entries(entry.defaults)) {
    const oldValue = nextContext[key];
    if (shouldFillEmptyActionValue(oldValue)) {
      a2uiDebug('[A2UI-AD] enriching visible default:', { key, defaultValue: value });
      nextContext[key] = value;
      changed = true;
    }
  }

  if (!changed) {
    return message;
  }

  return {
    ...message,
    userAction: {
      ...userAction,
      context: nextContext,
    },
  };
}
