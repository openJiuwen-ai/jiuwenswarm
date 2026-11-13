type Child = Node | string | false | null | undefined;
type Props = Record<string, string | boolean | ((event: Event) => void) | undefined>;

/** An element with attributes, ``on*`` listeners and children; ``false``, ``null`` and ``undefined`` children are left out. */
export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K, props: Props = {}, ...children: Child[]
): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(props)) {
    if (typeof value === 'function') element.addEventListener(name.slice(2).toLowerCase(), value);
    else if (value === true) element.setAttribute(name, '');
    else if (typeof value === 'string') element.setAttribute(name, value);
  }
  for (const child of children) {
    if (child !== false && child !== null && child !== undefined) element.append(child);
  }
  return element;
}

/**
 * Make ``nodes`` the children of ``parent``, moving only the ones out of
 * place, so a node that stays keeps its focus, selection and scroll.
 */
export function syncChildren(parent: Element, nodes: readonly Node[]): void {
  nodes.forEach((node, index) => {
    if (parent.childNodes[index] !== node) parent.insertBefore(node, parent.childNodes[index] ?? null);
  });
  while (parent.childNodes.length > nodes.length) parent.lastChild!.remove();
}
