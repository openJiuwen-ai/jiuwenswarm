import type { Html, Parents, PhrasingContent, Root, RootContent, Text } from 'mdast';
import { visit } from 'unist-util-visit';

type UnderlineNode = {
  type: 'underline';
  children: PhrasingContent[];
  data?: { hName: 'u' };
};

function isOpenU(node: RootContent | PhrasingContent | undefined): node is Html {
  return Boolean(node && node.type === 'html' && /^<u\s*>$/i.test(String(node.value || '').trim()));
}

function isCloseU(node: RootContent | PhrasingContent | undefined): node is Html {
  return Boolean(node && node.type === 'html' && /^<\/u\s*>$/i.test(String(node.value || '').trim()));
}

function singleUnderlineHtml(node: RootContent | PhrasingContent | undefined): Text | null {
  if (!node || node.type !== 'html') return null;
  const matched = /^<u\s*>([\s\S]*)<\/u>$/i.exec(String(node.value || '').trim());
  if (!matched) return null;
  return { type: 'text', value: matched[1] ?? '' };
}

/**
 * Collapse remark's split `<u>` / text / `</u>` html nodes into a real `<u>` element
 * via `data.hName`, without enabling full raw HTML (rehype-raw).
 */
export function remarkSafeUnderline() {
  return (tree: Root) => {
    visit(tree, (node) => {
      const parent = node as Parents;
      if (!('children' in parent) || !Array.isArray(parent.children)) return;
      const children = parent.children as Array<RootContent | PhrasingContent>;

      for (let i = 0; i < children.length; i += 1) {
        const whole = singleUnderlineHtml(children[i]);
        if (whole) {
          const underline: UnderlineNode = {
            type: 'underline',
            children: [whole],
            data: { hName: 'u' },
          };
          children[i] = underline as unknown as (typeof children)[number];
          continue;
        }

        if (!isOpenU(children[i])) continue;
        let closeAt = -1;
        for (let j = i + 1; j < children.length; j += 1) {
          if (isCloseU(children[j])) {
            closeAt = j;
            break;
          }
          if (isOpenU(children[j])) break;
        }
        if (closeAt < 0) continue;

        const inner = children.slice(i + 1, closeAt) as PhrasingContent[];
        const underline: UnderlineNode = {
          type: 'underline',
          children: inner,
          data: { hName: 'u' },
        };
        children.splice(i, closeAt - i + 1, underline as unknown as (typeof children)[number]);
      }
    });
  };
}
