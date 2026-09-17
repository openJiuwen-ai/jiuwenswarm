import type { PluggableList } from 'unified';
import rehypeKatex from 'rehype-katex';
import type { Options as RehypeKatexOptions } from 'rehype-katex';
import rehypeSlug from 'rehype-slug';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import { remarkLatexDelimiters } from './math/remarkLatexDelimiters';

const KATEX_OPTIONS: RehypeKatexOptions = { trust: false };

export const MARKDOWN_REMARK_PLUGINS: PluggableList = [remarkGfm, remarkMath, remarkLatexDelimiters];
export const MARKDOWN_REHYPE_PLUGINS: PluggableList = [rehypeSlug, [rehypeKatex, KATEX_OPTIONS]];
