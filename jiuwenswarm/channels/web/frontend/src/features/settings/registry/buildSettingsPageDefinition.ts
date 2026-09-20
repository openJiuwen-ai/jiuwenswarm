import { createSettingsPageDefinition } from './createSettingsPageDefinition';
import type {
  SettingItemDefinition,
  SettingsAccessBinding,
  SettingsAccessLevel,
  SettingsAccessNode,
  SettingsAccessPolicy,
  SettingsAccessResult,
  SettingsCapabilitySnapshot,
  SettingsCompositionMode,
  SettingsModuleDefinition,
  SettingsPageDefinition,
  SettingsPageOverlay,
} from './types';

const ACCESS_LEVEL_RANK: Readonly<Record<SettingsAccessLevel, number>> = {
  hidden: 0,
  readOnly: 1,
  editable: 2,
};

function assertNonEmpty(label: string, value: string): void {
  if (!value.trim()) throw new Error(`${label} must not be empty`);
}

function accessNodeKey(node: SettingsAccessNode): string {
  if (node.kind === 'module') return `module:${node.moduleId}`;
  if (node.kind === 'section') return `section:${node.moduleId}/${node.sectionId}`;
  return `item:${node.moduleId}/${node.sectionId}/${node.itemId}`;
}

function assertTargetExists(modules: readonly SettingsModuleDefinition[], target: SettingsAccessNode): void {
  const module = modules.find((candidate) => candidate.id === target.moduleId);
  if (!module) throw new Error(`Settings access target does not exist: ${accessNodeKey(target)}`);
  if (target.kind === 'module') return;
  const section = module.sections.find((candidate) => candidate.id === target.sectionId);
  if (!section) throw new Error(`Settings access target does not exist: ${accessNodeKey(target)}`);
  if (target.kind === 'section') return;
  if (!section.items.some((candidate) => candidate.id === target.itemId)) {
    throw new Error(`Settings access target does not exist: ${accessNodeKey(target)}`);
  }
}

function addModule(
  modules: SettingsModuleDefinition[],
  contribution: NonNullable<SettingsPageOverlay['addModules']>[number],
  lastInsertedByAnchor: Map<string, string>,
): void {
  const { module, afterModuleId } = contribution;
  if (modules.some((candidate) => candidate.id === module.id)) {
    throw new Error(`Duplicate module id: ${module.id}`);
  }
  if (afterModuleId === undefined) {
    modules.push(module);
    return;
  }
  const effectiveAnchorId = lastInsertedByAnchor.get(afterModuleId) ?? afterModuleId;
  const anchorIndex = modules.findIndex((candidate) => candidate.id === effectiveAnchorId);
  if (anchorIndex < 0) {
    throw new Error(`Settings module ${module.id} references missing afterModuleId: ${afterModuleId}`);
  }
  modules.splice(anchorIndex + 1, 0, module);
  lastInsertedByAnchor.set(afterModuleId, module.id);
}

function replaceItem(
  modules: SettingsModuleDefinition[],
  target: Extract<SettingsAccessNode, { kind: 'item' }>,
  replacement: SettingItemDefinition,
): void {
  const moduleIndex = modules.findIndex((candidate) => candidate.id === target.moduleId);
  const module = modules[moduleIndex];
  const sectionIndex = module?.sections.findIndex((candidate) => candidate.id === target.sectionId) ?? -1;
  const section = sectionIndex >= 0 ? module.sections[sectionIndex] : undefined;
  const itemIndex = section?.items.findIndex((candidate) => candidate.id === target.itemId) ?? -1;
  const item = itemIndex >= 0 ? section?.items[itemIndex] : undefined;
  if (!item || !section || !module) {
    throw new Error(`Settings replacement target does not exist: ${accessNodeKey(target)}`);
  }
  if (!item.replaceable) {
    throw new Error(`Settings replacement target is not replaceable: ${accessNodeKey(target)}`);
  }
  if (replacement.id !== item.id) {
    throw new Error(`Settings replacement must preserve item id: ${item.id}`);
  }
  const sections = [...module.sections];
  const items = [...section.items];
  items[itemIndex] = replacement;
  sections[sectionIndex] = { ...section, items };
  modules[moduleIndex] = { ...module, sections };
}

function createCapabilityAccessPolicy({
  basePolicy,
  bindings,
  snapshot,
}: {
  basePolicy: SettingsAccessPolicy;
  bindings: readonly SettingsAccessBinding[];
  snapshot: SettingsCapabilitySnapshot;
}): SettingsAccessPolicy {
  assertNonEmpty('Settings capability snapshot revision', snapshot.revision);
  if (!snapshot.capabilities || typeof snapshot.capabilities !== 'object' || Array.isArray(snapshot.capabilities)) {
    throw new Error('Settings capability snapshot capabilities must be an object');
  }
  for (const [capability, level] of Object.entries(snapshot.capabilities)) {
    assertNonEmpty('Settings capability', capability);
    if (!Object.prototype.hasOwnProperty.call(ACCESS_LEVEL_RANK, level)) {
      throw new Error(`Settings capability ${capability} has invalid access level: ${String(level)}`);
    }
  }
  const bindingsByTarget = new Map<string, SettingsAccessBinding>();
  for (const binding of bindings) {
    assertNonEmpty('Settings capability', binding.capability);
    const targetKey = accessNodeKey(binding.target);
    if (bindingsByTarget.has(targetKey)) throw new Error(`Duplicate settings access binding: ${targetKey}`);
    if (!Object.prototype.hasOwnProperty.call(snapshot.capabilities, binding.capability)) {
      throw new Error(`Settings capability snapshot is missing capability: ${binding.capability}`);
    }
    bindingsByTarget.set(targetKey, binding);
  }
  return {
    evaluate(node, context) {
      const base = basePolicy.evaluate(node, context);
      const binding = bindingsByTarget.get(accessNodeKey(node));
      if (!binding) return base;
      const capabilityLevel = snapshot.capabilities[binding.capability];
      if (ACCESS_LEVEL_RANK[capabilityLevel] >= ACCESS_LEVEL_RANK[base.level]) return base;
      return { level: capabilityLevel, reasonKey: binding.reasonKey };
    },
  };
}

export function restrictSettingsAccess(...results: readonly SettingsAccessResult[]): SettingsAccessResult {
  let effective = results[0] ?? { level: 'editable' as const };
  for (const result of results.slice(1)) {
    if (ACCESS_LEVEL_RANK[result.level] < ACCESS_LEVEL_RANK[effective.level]) effective = result;
  }
  return effective;
}

export function buildSettingsPageDefinition({
  id,
  compositionMode,
  base,
  overlays,
  capabilitySnapshot,
}: {
  id: string;
  compositionMode: SettingsCompositionMode;
  base: SettingsPageDefinition;
  overlays: readonly SettingsPageOverlay[];
  capabilitySnapshot?: SettingsCapabilitySnapshot;
}): SettingsPageDefinition {
  assertNonEmpty('Settings page id', id);
  if (compositionMode === 'base') {
    if (overlays.length > 0 || capabilitySnapshot !== undefined) {
      throw new Error('Settings overlays and capability snapshots require extended composition');
    }
    if (base.compositionMode !== 'base') throw new Error('Base settings composition requires a base definition');
    return createSettingsPageDefinition({ ...base, id, compositionMode });
  }
  if (!capabilitySnapshot) throw new Error('Extended settings composition requires a capability snapshot');
  if (base.compositionMode !== 'base') throw new Error('Extended settings composition requires a base definition');

  const modules = [...base.modules];
  const lastInsertedByAnchor = new Map<string, string>();
  const overlayIds = new Set<string>();
  const replacementTargets = new Set<string>();
  const bindings: SettingsAccessBinding[] = [];
  const requiredI18nKeys = [...(base.requiredI18nKeys ?? [])];

  for (const overlay of overlays) {
    assertNonEmpty('Settings overlay id', overlay.id);
    if (overlayIds.has(overlay.id)) throw new Error(`Duplicate settings overlay id: ${overlay.id}`);
    overlayIds.add(overlay.id);
    for (const contribution of overlay.addModules ?? []) {
      addModule(modules, contribution, lastInsertedByAnchor);
      bindings.push({
        target: { kind: 'module', moduleId: contribution.module.id },
        ...contribution.access,
      });
    }
    for (const replacement of overlay.replaceItems ?? []) {
      const targetKey = accessNodeKey(replacement.target);
      if (replacementTargets.has(targetKey)) throw new Error(`Duplicate settings replacement target: ${targetKey}`);
      replacementTargets.add(targetKey);
      replaceItem(modules, replacement.target, replacement.item);
    }
    bindings.push(...(overlay.accessBindings ?? []));
    requiredI18nKeys.push(...(overlay.requiredI18nKeys ?? []));
  }

  for (const binding of bindings) assertTargetExists(modules, binding.target);
  const accessPolicy = createCapabilityAccessPolicy({
    basePolicy: base.accessPolicy,
    bindings,
    snapshot: capabilitySnapshot,
  });
  return createSettingsPageDefinition({
    id,
    compositionMode,
    modules,
    accessPolicy,
    requiredI18nKeys,
  });
}
