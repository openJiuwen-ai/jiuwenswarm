import type { PluggableList } from 'unified';
import type { Parent, Root } from 'mdast';
import type { VFile } from 'vfile';
import rehypeKatex from 'rehype-katex';
import type { Options as RehypeKatexOptions } from 'rehype-katex';
import rehypeSlug from 'rehype-slug';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { remarkLatexDelimiters } from './math/remarkLatexDelimiters';

const KATEX_OPTIONS: RehypeKatexOptions = { trust: false };
const KATEX_HTML_ONLY_OPTIONS: RehypeKatexOptions = { trust: false, output: 'html' };

function remarkSourceLinkPunctuation() {
  return (tree: Root, file: VFile) => {
    const source = String(file.value);
    const visit = (parent: Parent, openParentheses = 0): number => {
      for (let index = 0; index < parent.children.length; index += 1) {
        const node = parent.children[index];
        const label =
          (node.type === 'link' || node.type === 'linkReference') && node.children.length === 1
            ? node.children[0]
            : undefined;
        const isUrlLabel =
          label?.type === 'text' && (label.value.startsWith('https://') || label.value.startsWith('http://'));
        if (node.type === 'text') {
          for (const character of node.value) {
            if (character === '（') openParentheses += 1;
            if (character === '）') openParentheses = Math.max(0, openParentheses - 1);
          }
        } else if (node.type === 'link') {
          const offset = node.position?.start.offset;
          const delimiter = offset === undefined ? '' : source[offset];
          if (openParentheses > 0 && isUrlLabel && delimiter !== '[' && delimiter !== '<') {
            let insideUrl = 0;
            for (let end = 0; end < label.value.length; end += 1) {
              const character = label.value[end];
              if (character === '（') insideUrl += 1;
              if (character !== '）') continue;
              if (insideUrl > 0) {
                insideUrl -= 1;
                continue;
              }
              const trailingText = label.value.slice(end);
              label.value = label.value.slice(0, end);
              node.url = label.value;
              parent.children.splice(index + 1, 0, { type: 'text', value: trailingText });
              break;
            }
          }
        }
        if ('children' in node) {
          if (isUrlLabel) continue;
          if (
            node.type === 'emphasis' ||
            node.type === 'strong' ||
            node.type === 'delete' ||
            node.type === 'link' ||
            node.type === 'linkReference'
          ) {
            openParentheses = visit(node, openParentheses);
          } else {
            visit(node);
          }
        }
      }
      return openParentheses;
    };
    visit(tree);
  };
}

export const MARKDOWN_REMARK_PLUGINS: PluggableList = [
  remarkGfm,
  remarkMath,
  remarkLatexDelimiters,
  remarkSourceLinkPunctuation,
];
export const MARKDOWN_REHYPE_PLUGINS: PluggableList = [rehypeSlug, [rehypeKatex, KATEX_OPTIONS]];
export const MARKDOWN_REHYPE_HTML_ONLY_PLUGINS: PluggableList = [rehypeSlug, [rehypeKatex, KATEX_HTML_ONLY_OPTIONS]];
