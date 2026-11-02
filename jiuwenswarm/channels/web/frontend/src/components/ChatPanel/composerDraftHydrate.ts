/**
 * Rebuild composer chips after session switches.
 *
 * Plain ``inputValue`` stores slash chips as ``/<name>…`` text and omits skill
 * chips entirely (skills live in sessionStore.selectedSkills). Setting
 * ``textContent`` alone therefore drops the blue /persist chip (#5771).
 */

/** Slash commands that become contenteditable=false chips while drafting. */
export const COMPOSER_SLASH_CHIP_COMMANDS: readonly string[] = ['persist'];

export function isComposerSlashChipCommand(name: string): boolean {
  const lower = name.trim().toLowerCase();
  return COMPOSER_SLASH_CHIP_COMMANDS.some((command) => command === lower);
}

/**
 * If ``text`` starts with a chipable slash command, split it into the command
 * name and the remaining plain text (including any leading whitespace/args).
 */
export function splitComposerSlashChip(text: string): { command: string | null; rest: string } {
  const match = /^\/([^\s/]+)([\s\S]*)$/.exec(text);
  if (!match) return { command: null, rest: text };
  const name = match[1].toLowerCase();
  if (!isComposerSlashChipCommand(name)) return { command: null, rest: text };
  return { command: name, rest: match[2] };
}

function composerNodeText(node: Node): string {
  if (node.nodeType === 1) {
    const element = node as HTMLElement;
    if (element.getAttribute('contenteditable') === 'false') {
      if (element.dataset.slashCommand) return `/${element.dataset.slashCommand}`;
      if (element.dataset.composerToken) {
        const prefix = element.dataset.composerToken === 'role' ? '$' : '@';
        return `${prefix}${element.dataset.value || element.textContent || ''}`;
      }
      return '';
    }
    return Array.from(node.childNodes, composerNodeText).join('');
  }
  return (node.textContent || '').replace(/\u200B/g, '');
}

export function readComposerText(editor: HTMLElement): string {
  return Array.from(editor.childNodes, composerNodeText).join('').replace(/\u200B/g, '');
}

/** Capture skill locations in the same plain-text coordinate system as inputValue. */
export function readComposerSkillPositions(editor: HTMLElement): { name: string; offset: number }[] {
  const positions: { name: string; offset: number }[] = [];
  let offset = 0;
  const visit = (node: Node) => {
    if (node.nodeType === 1 && (node as HTMLElement).dataset.skill) {
      positions.push({ name: (node as HTMLElement).dataset.skill!, offset });
    } else if (node.nodeType === 1 && (node as HTMLElement).getAttribute('contenteditable') !== 'false') {
      node.childNodes.forEach(visit);
      return;
    }
    offset += composerNodeText(node).length;
  };
  editor.childNodes.forEach(visit);
  return positions;
}

/** Restore at a text offset, ignoring skill chips and caret spacer characters. */
export function createComposerSkillRange(editor: HTMLElement, offset: number): Range {
  const range = editor.ownerDocument.createRange();
  let remaining = Math.max(0, offset);
  const nodes: Node[] = [];
  const visit = (node: Node) => {
    if (node.nodeType === 1 && (node as HTMLElement).getAttribute('contenteditable') !== 'false') {
      node.childNodes.forEach(visit);
    } else {
      nodes.push(node);
    }
  };
  editor.childNodes.forEach(visit);
  for (const node of nodes) {
    const text = composerNodeText(node);
    if (!text.length) continue;
    if (remaining < text.length) {
      if (node.nodeType === 3) {
        const raw = node.textContent || '';
        let index = 0;
        let count = 0;
        while (index < raw.length && count < remaining) {
          if (raw[index] !== '\u200B') count += 1;
          index += 1;
        }
        range.setStart(node, index);
      } else if (remaining === 0) {
        range.setStartBefore(node);
      } else {
        range.setStartAfter(node);
      }
      range.collapse(true);
      return range;
    }
    remaining -= text.length;
  }
  range.selectNodeContents(editor);
  range.collapse(false);
  return range;
}
