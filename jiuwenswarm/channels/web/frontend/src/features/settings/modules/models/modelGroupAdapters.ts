import type {
  ModelEntry,
  ModelGroup,
  ModelRoute,
  ModelSelection,
  ModelSelectionReference,
} from '../../../../types';

export function hasStableModelId(model: ModelEntry): model is ModelEntry & { model_id: string } {
  return typeof model.model_id === 'string' && model.model_id.trim().length > 0;
}

/** Zen free models (and any list row tagged is_free) cannot join a model group. */
export function isFreeModelEntry(model: ModelEntry): boolean {
  if (model.is_free === true) return true;
  const apiBase = (model.api_base || '').trim().toLowerCase().replace(/\/+$/, '');
  // Persisted Zen free entries keep the public gateway base even after gaining a model_id.
  if (apiBase === 'https://opencode.ai/zen/v1' || apiBase.includes('opencode.ai/zen')) {
    return true;
  }
  return false;
}

/** Models that can be referenced by a group route (including agentos; excluding free models). */
export function getGroupCandidateModels(models: readonly ModelEntry[]): Array<ModelEntry & { model_id: string }> {
  return models.filter((model): model is ModelEntry & { model_id: string } => {
    return hasStableModelId(model) && !isFreeModelEntry(model);
  });
}

export function parseModelSelection(value: unknown): ModelSelection | null {
  if (!isPlainObject(value)) return null;
  const type = value.type;
  const id = value.id;
  if ((type !== 'model' && type !== 'model_group') || typeof id !== 'string' || !id.trim()) return null;
  const normalizedId = id.trim();
  if (type === 'model') {
    if (value.route_id !== undefined && value.route_id !== null) return null;
    return { type, id: normalizedId };
  }
  if (value.route_id === undefined || value.route_id === null) return { type, id: normalizedId };
  if (typeof value.route_id !== 'string' || !value.route_id.trim()) return null;
  return { type, id: normalizedId, route_id: value.route_id.trim() };
}

export function selectionFromModel(model: ModelEntry | null | undefined): ModelSelection | null {
  if (!model || !hasStableModelId(model)) return null;
  return { type: 'model', id: model.model_id };
}

export function getModelSelectionLabel(
  selection: ModelSelection | null | undefined,
  models: readonly ModelEntry[],
  groups: readonly ModelGroup[],
): string | null {
  if (!selection) return null;
  if (selection.type === 'model_group') {
    const group = groups.find((item) => item.model_group_id === selection.id);
    // Missing groups must never surface model_group_id in the picker (#5424).
    if (!group) return null;
    const groupLabel = getModelGroupDisplayName(group);
    if (!selection.route_id) return groupLabel || null;
    const route = group.routes.find((item) => item.route_id === selection.route_id);
    if (!route) return groupLabel || null;
    const model = models.find((item) => item.model_id === route.model_id);
    const routeLabel = model?.alias?.trim() || model?.model_name || route.model_id;
    return groupLabel ? `${groupLabel} / ${routeLabel}` : routeLabel;
  }
  const model = models.find((item) => item.model_id === selection.id);
  return model?.alias?.trim() || model?.model_name || null;
}

export function extractModelSelectionReferences(error: unknown): ModelSelectionReference[] {
  if (!error || typeof error !== 'object') return [];
  const payload = (error as { payload?: unknown }).payload;
  if (isPlainObject(payload)) return parseModelSelectionReferences(payload);
  return parseModelSelectionReferences(error);
}

/** Groups that still route to ``modelId`` (local catalog check before delete). */
export function findModelGroupReferencesForModel(
  modelId: string | undefined,
  groups: readonly ModelGroup[],
): ModelSelectionReference[] {
  const normalized = modelId?.trim();
  if (!normalized) return [];
  return groups
    .filter((group) => group.routes.some((route) => route.model_id === normalized))
    .map((group) => {
      const scopeId = group.model_group_id?.trim() || '';
      const label = getModelGroupDisplayName(group) || scopeId;
      const reference: ModelSelectionReference = { scope: 'model_group' };
      if (scopeId) reference.scope_id = scopeId;
      if (label) reference.label = label;
      return reference;
    });
}

/** Prefer display labels over stable mgp_/mdl_ business ids in delete warnings. */
export function formatModelSelectionReferenceScopes(
  references: readonly ModelSelectionReference[],
  models: readonly ModelEntry[],
  groups: readonly ModelGroup[],
): string {
  return references
    .map((reference) => {
      const labeled = reference.label?.trim();
      if (labeled) return labeled;
      if (reference.scope === 'model_group' && reference.scope_id) {
        const group = groups.find((item) => item.model_group_id === reference.scope_id);
        if (group) return getModelGroupDisplayName(group) || reference.scope_id;
      }
      if ((reference.scope === 'model' || reference.scope === 'model_id') && reference.scope_id) {
        const model = models.find((item) => item.model_id === reference.scope_id);
        if (model) return model.alias?.trim() || model.model_name || reference.scope_id;
      }
      return reference.scope_id || reference.scope;
    })
    .filter(Boolean)
    .join(', ');
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function parseRoute(value: unknown): ModelRoute | null {
  if (!isPlainObject(value)) return null;
  const modelId = value.model_id;
  if (typeof modelId !== 'string' || !modelId.trim()) return null;
  const route: ModelRoute = { model_id: modelId.trim() };
  if (typeof value.route_id === 'string' && value.route_id.trim()) route.route_id = value.route_id.trim();
  if (typeof value.enabled === 'boolean') route.enabled = value.enabled;
  if (isPlainObject(value.request_overrides)) route.request_overrides = value.request_overrides;
  if (typeof value.tpm === 'number') route.tpm = value.tpm;
  if (typeof value.rpm === 'number') route.rpm = value.rpm;
  if (typeof value.timeout === 'number') route.timeout = value.timeout;
  return route;
}

export function parseModelGroup(value: unknown): ModelGroup | null {
  if (!isPlainObject(value)) return null;
  const routesRaw = value.routes;
  if (!Array.isArray(routesRaw)) return null;
  const routes = routesRaw.map(parseRoute).filter((route): route is ModelRoute => route !== null);
  const group: ModelGroup = { routes };
  if (typeof value.model_group_id === 'string' && value.model_group_id.trim()) {
    group.model_group_id = value.model_group_id.trim();
  }
  if (value.display_name === null || typeof value.display_name === 'string') {
    group.display_name = value.display_name;
  }
  if (value.enabled === null || typeof value.enabled === 'boolean') group.enabled = value.enabled;
  if (value.is_default === null || typeof value.is_default === 'boolean') group.is_default = value.is_default;
  if (value.request_config === null || isPlainObject(value.request_config)) {
    group.request_config = value.request_config;
  }
  return group;
}

export function parseModelGroups(value: unknown): ModelGroup[] {
  if (!Array.isArray(value)) return [];
  return value.map(parseModelGroup).filter((group): group is ModelGroup => group !== null);
}

export function parseModelSelectionReferences(value: unknown): ModelSelectionReference[] {
  if (!isPlainObject(value) || !Array.isArray(value.references)) return [];
  return value.references
    .map((item) => {
      if (!isPlainObject(item) || typeof item.scope !== 'string') return null;
      const reference: ModelSelectionReference = { scope: item.scope };
      if (typeof item.scope_id === 'string') reference.scope_id = item.scope_id;
      if (typeof item.label === 'string' && item.label.trim()) reference.label = item.label.trim();
      return reference;
    })
    .filter((item): item is ModelSelectionReference => item !== null);
}

export function buildModelGroupUpsertPayload(input: {
  displayName: string;
  routes: ModelRoute[];
  enabled?: boolean;
  isDefault?: boolean;
  requestConfig?: Record<string, unknown> | null;
  group?: ModelGroup | null;
}): ModelGroup {
  const payload: ModelGroup = {
    display_name: input.displayName.trim(),
    enabled: input.enabled ?? input.group?.enabled !== false,
    is_default: input.isDefault ?? input.group?.is_default === true,
    routes: input.routes.map((route) => ({ ...route })),
  };
  if (input.group?.model_group_id) payload.model_group_id = input.group.model_group_id;
  const requestConfig = input.requestConfig === undefined ? input.group?.request_config : input.requestConfig;
  if (requestConfig !== undefined) payload.request_config = requestConfig;
  return payload;
}

export function getModelGroupDisplayName(group: ModelGroup): string {
  const name = group.display_name?.trim();
  return name || group.model_group_id || '';
}

export function formatModelGroupRouteSummary(
  group: ModelGroup,
  models: readonly ModelEntry[],
): string {
  const labels = group.routes.map((route) => {
    const model = models.find((entry) => entry.model_id === route.model_id);
    return model?.alias?.trim() || model?.model_name || route.model_id;
  });
  return labels.join(' → ');
}

/** First enabled route is used when no explicit route is selected. */
export function getGroupDefaultRouteId(group: ModelGroup): string | undefined {
  const route = group.routes.find((item) => item.enabled !== false) ?? group.routes[0];
  return route?.route_id;
}

/** Move the target route to the front so it becomes the in-group default. */
export function promoteGroupRouteDefault(group: ModelGroup, routeId: string): ModelGroup {
  const target = group.routes.find((route) => route.route_id === routeId);
  if (!target) return group;
  return {
    ...group,
    routes: [target, ...group.routes.filter((route) => route.route_id !== routeId)],
  };
}

/** Remove a route from the group. Caller must keep at least one route. */
export function removeGroupRoute(group: ModelGroup, routeId: string): ModelGroup {
  return {
    ...group,
    routes: group.routes.filter((route) => route.route_id !== routeId),
  };
}
