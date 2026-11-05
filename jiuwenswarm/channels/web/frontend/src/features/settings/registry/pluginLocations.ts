import type {
  SettingsModuleDefinition,
  SettingsPageDefinition,
  SettingsSectionDefinition,
  SettingItemDefinition,
} from './types';

export type SettingsLocation = { menu?: string; section?: string; before?: string; after?: string };
type Located<T> = { value: T; location: SettingsLocation };
export type SettingsContribution =
  | (Located<SettingsModuleDefinition> & { kind: 'menu' })
  | (Located<SettingsSectionDefinition> & { kind: 'section' })
  | (Located<SettingItemDefinition> & { kind: 'item' });
export type LocationError = { id: string; code: 'DUPLICATE_ID' | 'PARENT_MISSING' | 'ORDER_CYCLE' };

/** Native siblings define insertion gaps. Plugin anchors inherit their anchor's gap. */
export function orderSettingsNodes<T extends { id: string }>(native: readonly T[], added: readonly Located<T>[]) {
  const errors: LocationError[] = [];
  const nativeIndex = new Map(native.map((node, index) => [node.id, index]));
  const counts = new Map<string, number>();
  added.forEach(({ value }) => counts.set(value.id, (counts.get(value.id) ?? 0) + 1));
  const valid = added.filter(({ value }) => {
    if (nativeIndex.has(value.id) || counts.get(value.id)! > 1) {
      errors.push({ id: value.id, code: 'DUPLICATE_ID' });
      return false;
    }
    return true;
  });
  const nodes = new Map(valid.map((entry, index) => [entry.value.id, { ...entry, index }]));
  const gaps = new Map<string, number>();
  for (const start of nodes.keys()) {
    const path: string[] = [];
    const visited = new Set<string>();
    let cursor = start;
    let gap = native.length;
    while (!gaps.has(cursor) && !visited.has(cursor)) {
      visited.add(cursor);
      path.push(cursor);
      const location = nodes.get(cursor)!.location;
      const anchor = location.before ?? location.after;
      if (anchor === undefined) break;
      const position = nativeIndex.get(anchor);
      if (position !== undefined) {
        gap = position + (location.after !== undefined ? 1 : 0);
        break;
      }
      if (!nodes.has(anchor)) break;
      cursor = anchor;
    }
    if (gaps.has(cursor)) gap = gaps.get(cursor)!;
    path.forEach((id) => gaps.set(id, gap));
  }
  const values: T[] = [];
  for (let gap = 0; gap <= native.length; gap++) {
    const group = valid.filter(({ value }) => gaps.get(value.id) === gap);
    const outgoing = new Map(group.map(({ value }) => [value.id, new Set<string>()]));
    const degrees = new Map(group.map(({ value }) => [value.id, 0]));
    for (const { value, location } of group) {
      const anchor = location.before ?? location.after;
      if (anchor === undefined || !outgoing.has(anchor)) continue;
      const from = location.before !== undefined ? value.id : anchor;
      const to = location.before !== undefined ? anchor : value.id;
      if (!outgoing.get(from)!.has(to)) {
        outgoing.get(from)!.add(to);
        degrees.set(to, degrees.get(to)! + 1);
      }
    }
    const ready = group.filter(({ value }) => degrees.get(value.id) === 0).map(({ value }) => value.id);
    while (ready.length) {
      ready.sort((a, b) => nodes.get(a)!.index - nodes.get(b)!.index);
      const id = ready.shift()!;
      values.push(nodes.get(id)!.value);
      degrees.delete(id);
      for (const next of outgoing.get(id)!) {
        degrees.set(next, degrees.get(next)! - 1);
        if (degrees.get(next) === 0) ready.push(next);
      }
    }
    degrees.forEach((_degree, id) => errors.push({ id, code: 'ORDER_CYCLE' }));
    if (gap < native.length) values.push(native[gap]);
  }
  return { values, errors };
}

export function composePluginSettings(base: SettingsPageDefinition, contributions: readonly SettingsContribution[]) {
  const menus = orderSettingsNodes(
    base.modules,
    contributions.filter((c): c is Extract<SettingsContribution, { kind: 'menu' }> => c.kind === 'menu'),
  );
  const errors = [...menus.errors];
  const menuIds = new Set(menus.values.map((menu) => menu.id));
  for (const c of contributions) {
    if (c.kind !== 'menu' && !menuIds.has(c.location.menu!)) errors.push({ id: c.value.id, code: 'PARENT_MISSING' });
  }
  const modules = menus.values.map((menu) => {
    const sections = orderSettingsNodes(
      menu.sections,
      contributions.filter(
        (c): c is Extract<SettingsContribution, { kind: 'section' }> =>
          c.kind === 'section' && c.location.menu === menu.id,
      ),
    );
    errors.push(...sections.errors);
    const sectionIds = new Set(sections.values.map((section) => section.id));
    for (const c of contributions) {
      if (c.kind === 'item' && c.location.menu === menu.id && !sectionIds.has(c.location.section!)) {
        errors.push({ id: c.value.id, code: 'PARENT_MISSING' });
      }
    }
    return {
      ...menu,
      sections: sections.values.map((section) => {
        const items = orderSettingsNodes(
          section.items,
          contributions.filter(
            (c): c is Extract<SettingsContribution, { kind: 'item' }> =>
              c.kind === 'item' && c.location.menu === menu.id && c.location.section === section.id,
          ),
        );
        errors.push(...items.errors);
        return { ...section, items: items.values };
      }),
    };
  });
  return { definition: { ...base, modules }, errors };
}
