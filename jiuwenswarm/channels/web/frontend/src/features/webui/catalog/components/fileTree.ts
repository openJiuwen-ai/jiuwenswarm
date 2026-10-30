import type { WebUIPropRule } from '../types.ts';
import { array, bool, component, nil, object, text, token, union, unique } from './shared.ts';

export interface WebUIFileTreeNode {
  key: string;
  title: string;
  path: string;
  kind: 'file' | 'directory';
  children?: WebUIFileTreeNode[];
  loaded?: boolean;
  truncated?: boolean;
}

export function validWorkspacePath(value: string): boolean {
  return value.length > 0 && !value.includes('\\') && !value.includes('\0')
    && !value.startsWith('/') && !value.includes(':')
    && value.split('/').every((part) => part.length > 0 && part !== '.' && part !== '..');
}

const fileNode: WebUIPropRule = object({
  key: text, title: text, path: text, kind: token('file', 'directory'),
  children: array({ kind: 'lazy', get: () => fileNode }), loaded: bool, truncated: bool,
}, ['key', 'title', 'path', 'kind']);
const expandedKeysRule: WebUIPropRule = { kind: 'array', item: text, maxItems: 100 };

export const fileTreeRule = component('FileTree', {
  treeData: array(fileNode), selectedKey: union(text, nil), expandedKeys: expandedKeysRule,
  disabled: bool, ariaLabel: text, emptyText: text, message: text,
  status: token('ready', 'unavailable', 'unsupported', 'error'), truncated: bool, readFailed: bool,
}, {
  required: ['treeData'],
  events: {
    'ui.select': object({ key: text }, ['key']),
    'ui.change': object({ key: text, expanded: bool, expandedKeys: expandedKeysRule }, ['key', 'expanded']),
  },
  validate(p) {
    const nodes = new Map<string, WebUIFileTreeNode>();
    const paths = new Set<string>();
    let error: string | null = null;
    function walk(items: WebUIFileTreeNode[], depth: number, parent?: string): void {
      if (items.length && depth > 8) { error = 'file tree is too deep'; return; }
      for (const item of items) {
        if (!item.key || nodes.has(item.key) || !validWorkspacePath(item.path) || paths.has(item.path)
          || (parent !== undefined && !item.path.startsWith(`${parent}/`))) {
          error = 'invalid file tree identities or paths'; return;
        }
        nodes.set(item.key, item); paths.add(item.path);
        if (nodes.size > 512) { error = 'file tree has too many nodes'; return; }
        if (item.kind === 'file' && (item.children !== undefined || item.loaded !== undefined || item.truncated !== undefined)) {
          error = 'file nodes cannot declare directory fields'; return;
        }
        if (item.loaded === false && item.children !== undefined) { error = 'unloaded directory has children'; return; }
        walk(item.children ?? [], depth + 1, item.path);
        if (error) return;
      }
    }
    walk(p.treeData as WebUIFileTreeNode[], 1);
    if (error) return error;
    if (p.selectedKey !== undefined && p.selectedKey !== null
      && nodes.get(p.selectedKey as string)?.kind !== 'file') return 'unknown selected file';
    const expanded = (p.expandedKeys ?? []) as string[];
    return !unique(expanded) || expanded.some((key) => nodes.get(key)?.kind !== 'directory')
      ? 'unknown expanded directory' : null;
  },
  validateEvent(event, data, p) {
    if (p.disabled === true || (p.status !== undefined && p.status !== 'ready')) return 'file tree is unavailable';
    const nodes = new Map<string, WebUIFileTreeNode>();
    function walk(items: WebUIFileTreeNode[]): void {
      for (const item of items) { nodes.set(item.key, item); walk(item.children ?? []); }
    }
    walk(p.treeData as WebUIFileTreeNode[]);
    const item = nodes.get(data.key as string);
    if (event === 'ui.select') return item?.kind === 'file' ? null : 'unknown selected file';
    if (event !== 'ui.change' || item?.kind !== 'directory') return 'unknown expanded directory';
    if (data.expandedKeys !== undefined) {
      const expanded = data.expandedKeys as string[];
      if (!unique(expanded) || expanded.some((key) => nodes.get(key)?.kind !== 'directory')) {
        return 'unknown expanded directory';
      }
      if (expanded.includes(data.key as string) !== data.expanded) return 'expanded directory state does not match';
    }
    return null;
  },
});
