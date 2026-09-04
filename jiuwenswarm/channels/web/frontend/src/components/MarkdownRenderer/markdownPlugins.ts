import type { PluggableList } from 'unified';
import rehypeKatex from 'rehype-katex';
import type { Options as RehypeKatexOptions } from 'rehype-katex';
import rehypeSlug from 'rehype-slug';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { remarkLatexDelimiters } from './math/remarkLatexDelimiters';

const KATEX_OPTIONS: RehypeKatexOptions = { trust: false };
const KATEX_HTML_ONLY_OPTIONS: RehypeKatexOptions = { trust: false, output: 'html' };

export const MARKDOWN_REMARK_PLUGINS: PluggableList = [remarkGfm, remarkMath, remarkLatexDelimiters];
export const MARKDOWN_REHYPE_PLUGINS: PluggableList = [rehypeSlug, [rehypeKatex, KATEX_OPTIONS]];
export const MARKDOWN_REHYPE_HTML_ONLY_PLUGINS: PluggableList = [rehypeSlug, [rehypeKatex, KATEX_HTML_ONLY_OPTIONS]];
