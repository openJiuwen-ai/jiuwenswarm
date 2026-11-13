import { h } from './dom';

/**
 * The Markdown a reply uses, as DOM nodes built from text: paragraphs, bullet
 * and numbered lists, headings, bold and inline code. Nothing is parsed as
 * HTML, so a reply cannot inject markup.
 */
export function renderMarkdown(content: string): Node[] {
  const nodes: Node[] = [];
  let paragraph: HTMLParagraphElement | null = null;
  let list: HTMLUListElement | HTMLOListElement | null = null;
  const flushParagraph = (): void => {
    if (paragraph) nodes.push(paragraph);
    paragraph = null;
  };
  const flushList = (): void => {
    if (list) nodes.push(list);
    list = null;
  };
  for (const line of content.replace(/\r\n?/g, '\n').split('\n')) {
    const bullet = /^\s*[-*]\s+(.+)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    if (bullet || numbered) {
      flushParagraph();
      const tag = numbered ? 'OL' : 'UL';
      if (list && list.tagName !== tag) flushList();
      list ??= numbered ? h('ol') : h('ul');
      list.append(h('li', {}, ...inline((numbered ?? bullet)![1])));
      continue;
    }
    flushList();
    if (!line.trim()) {
      flushParagraph();
      continue;
    }
    const heading = /^\s*#{1,6}\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      nodes.push(h('strong', { class: 'md-heading' }, ...inline(heading[1])));
      continue;
    }
    paragraph ??= h('p');
    if (paragraph.childNodes.length) paragraph.append(h('br'));
    paragraph.append(...inline(line));
  }
  flushParagraph();
  flushList();
  return nodes;
}

function inline(value: string): Node[] {
  const nodes: Node[] = [];
  let cursor = 0;
  for (const match of value.matchAll(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g)) {
    const index = match.index ?? 0;
    if (index > cursor) nodes.push(document.createTextNode(value.slice(cursor, index)));
    const token = match[0];
    nodes.push(token.startsWith('**') ? h('strong', {}, token.slice(2, -2)) : h('code', {}, token.slice(1, -1)));
    cursor = index + token.length;
  }
  if (cursor < value.length) nodes.push(document.createTextNode(value.slice(cursor)));
  return nodes;
}
