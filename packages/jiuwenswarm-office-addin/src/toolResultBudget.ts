type JsonObject = Record<string, unknown>;

interface ArrayRef {
  path: string;
  array: unknown[];
  parent: JsonObject | null;
  key: string | null;
  originalLength: number;
}

/**
 * Serialize a client tool result within `limit` characters while keeping it valid JSON.
 *
 * Tool results feed the model's context and server-side tools that `JSON.parse` them
 * (the visual review reads verify_slide_visual's geometry), so cutting the text
 * mid-string breaks every machine consumer. Instead, trailing items are dropped from the
 * largest leaf arrays (arrays whose items hold no non-empty array, such as a slide's
 * `shapes` or `overflows`), so a container like `slides` loses whole entries
 * only after its members' lists are exhausted. Each trimmed array gets a sibling
 * `<key>_omitted` count and the top level lists every trimmed path.
 */
export function fitToolResultJson(value: JsonObject, limit: number): string {
  const text = JSON.stringify(value);
  if (text.length <= limit) return text;

  const copy = JSON.parse(text) as JsonObject;
  const trimmed = new Map<unknown[], ArrayRef>();
  for (;;) {
    const candidate = largestLeafArray(copy);
    if (!candidate) break;
    const over = JSON.stringify(annotate(copy, trimmed)).length - limit;
    if (over <= 0) break;
    if (!trimmed.has(candidate.array)) trimmed.set(candidate.array, candidate);
    let freed = 0;
    while (candidate.array.length && freed < over) {
      freed += JSON.stringify(candidate.array.pop()).length + 1;
    }
  }
  const fitted = JSON.stringify(annotate(copy, trimmed));
  if (fitted.length <= limit) return fitted;
  return JSON.stringify({
    ...('success' in value ? { success: value.success } : {}),
    truncated: true,
    error: `Tool result was ${text.length} characters and could not be reduced below ${limit}; request a narrower range.`,
  });
}

function annotate(root: JsonObject, trimmed: Map<unknown[], ArrayRef>): JsonObject {
  if (!trimmed.size) return root;
  const omitted = [...trimmed.values()].map((ref) => {
    const count = ref.originalLength - ref.array.length;
    if (ref.parent && ref.key !== null) ref.parent[`${ref.key}_omitted`] = count;
    return { path: ref.path, kept: ref.array.length, omitted: count };
  });
  return { ...root, truncated: true, omitted };
}

function largestLeafArray(root: JsonObject): ArrayRef | null {
  let best: ArrayRef | null = null;
  let bestSize = 0;
  const visit = (node: unknown, path: string, parent: JsonObject | null, key: string | null): boolean => {
    // Returns whether `node` contains a non-empty array anywhere below it.
    if (Array.isArray(node)) {
      let nested = false;
      node.forEach((item, index) => { if (visit(item, `${path}[${index}]`, null, null)) nested = true; });
      if (!nested && node.length) {
        const size = JSON.stringify(node).length;
        if (size > bestSize) {
          bestSize = size;
          best = { path, array: node, parent, key, originalLength: node.length };
        }
      }
      return node.length > 0;
    }
    if (node && typeof node === 'object') {
      let nested = false;
      for (const [childKey, child] of Object.entries(node as JsonObject)) {
        if (visit(child, path ? `${path}.${childKey}` : childKey, node as JsonObject, childKey)) nested = true;
      }
      return nested;
    }
    return false;
  };
  visit(root, '', null, null);
  return best;
}
