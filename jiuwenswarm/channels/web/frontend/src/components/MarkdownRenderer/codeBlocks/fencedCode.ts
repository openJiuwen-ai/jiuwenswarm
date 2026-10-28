import { Children, isValidElement, type HTMLAttributes, type ReactElement, type ReactNode } from 'react';
import type { FencedCodeBlock } from './types';

interface MarkdownPositionPoint {
  line?: number;
}

export interface PositionedMarkdownNode {
  position?: {
    start?: MarkdownPositionPoint;
    end?: MarkdownPositionPoint;
  };
}

function getCodeElement(children: ReactNode): ReactElement<HTMLAttributes<HTMLElement>> | null {
  const childArray = Children.toArray(children);
  if (childArray.length !== 1) return null;

  const child = childArray[0];
  if (!isValidElement<HTMLAttributes<HTMLElement>>(child) || child.type !== 'code') return null;
  return child;
}

function getCodeLanguage(className: string): string | null {
  const languageClass = className.split(/\s+/).find(value => value.startsWith('language-'));
  const language = languageClass?.slice('language-'.length).trim();
  return language || null;
}

export function isCompleteCodeFence(contentLines: string[], node?: PositionedMarkdownNode): boolean {
  const startLine = node?.position?.start?.line;
  const endLine = node?.position?.end?.line;
  if (!startLine || !endLine) return false;

  const opener = contentLines[startLine - 1];
  const closer = contentLines[endLine - 1];
  if (!opener || !closer) return false;

  const openMatch = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(opener);
  if (!openMatch) return false;
  if (openMatch[1][0] === '`' && openMatch[2].includes('`')) return false;

  const fenceCharacter = openMatch[1][0];
  const fenceLength = openMatch[1].length;
  const closePattern = new RegExp(`^ {0,3}\\${fenceCharacter}{${fenceLength},}\\s*$`);
  return closePattern.test(closer);
}

export function getFencedCodeBlock(children: ReactNode, contentLines: string[], node?: PositionedMarkdownNode): FencedCodeBlock | null {
  const codeElement = getCodeElement(children);
  if (!codeElement) return null;

  const language = getCodeLanguage(codeElement.props.className || '');
  if (!language) return null;

  return {
    language,
    code: String(codeElement.props.children).replace(/\n$/, ''),
    complete: isCompleteCodeFence(contentLines, node),
  };
}
