interface ContainsNode {
  contains(node: Node | null): boolean;
}

/**
 * Return true only when every click boundary is mounted and the target is
 * outside all of them. Waiting for every boundary avoids closing a popover
 * while React is still attaching its trigger or panel ref.
 */
export function isClickOutside(target: Node | null, boundaries: readonly (ContainsNode | null)[]): boolean {
  if (!target || boundaries.some(boundary => boundary === null)) return false;
  return boundaries.every(boundary => !boundary?.contains(target));
}
