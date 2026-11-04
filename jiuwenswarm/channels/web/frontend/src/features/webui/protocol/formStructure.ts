import type { WebUINormalizedNode } from '../contracts/normalized.ts';

export function formFields(root: WebUINormalizedNode, name: string): string[] {
  const fields: string[] = [];
  function walk(node: WebUINormalizedNode, selected: boolean) {
    const inside = node.component === 'Form' ? node.props.name === name : selected;
    if (inside && node.component === 'Form.Item') fields.push(node.props.name as string);
    for (const child of [...node.children, ...Object.values(node.contentGroups ?? {}).flat()]) walk(child, inside);
  }
  walk(root, false);
  return fields;
}

/** Resolve references within one instance, before rendering any part of the tree. */
export function validateFormStructure(root: WebUINormalizedNode): string | null {
  const forms = new Map<string, Set<string>>();
  const references: string[] = [];
  function walk(node: WebUINormalizedNode, form?: string, trigger = false): string | null {
    if (trigger && (!['Box', 'Text', 'Avatar', 'Image', 'Tag'].includes(node.component)
      || Object.keys(node.events).length > 0
      || (node.component === 'Image' && node.props.preview !== false))) {
      return 'Dropdown trigger must contain only display components';
    }
    let current = form;
    if (node.component === 'Form') {
      current = node.props.name as string;
      if (form || forms.has(current)) return 'nested or duplicate Form';
      if (node.children.length === 0) return 'Form requires Form.Item children';
      forms.set(current, new Set());
    }
    if (node.component === 'Form.Item') {
      if (!current || node.children.length !== 1) return 'Form.Item requires one Input inside a Form';
      const fields = forms.get(current)!;
      const name = node.props.name as string;
      if (fields.has(name)) return 'duplicate field name';
      fields.add(name);
      if (Object.keys(node.children[0].events).length > 0) return 'Form inputs use local events only';
    }
    if (node.component === 'Button' && typeof node.props.form === 'string') references.push(node.props.form);
    if (node.component === 'Dropdown' && (node.props.icon ? node.children.length !== 0 : node.children.length !== 1)) {
      return 'Dropdown requires exactly one trigger source';
    }
    for (const child of [...node.children, ...Object.values(node.contentGroups ?? {}).flat()]) {
      const error = walk(child, current, trigger || node.component === 'Dropdown');
      if (error) return error;
    }
    return null;
  }
  return walk(root) ?? (references.some((name) => !forms.has(name)) ? 'unknown Form reference' : null);
}
