import { forwardRef, useEffect, useMemo, useRef, useState } from 'react';
import { applyStyle } from 'html-to-image/es/apply-style';
import { cloneNode as cloneHtmlNode } from 'html-to-image/es/clone-node';
import { embedImages } from 'html-to-image/es/embed-images';
import { embedWebFonts, getWebFontCSS } from 'html-to-image/es/embed-webfonts';
import type { Options as HtmlToImageOptions } from 'html-to-image/es/types';
import { useTranslation } from 'react-i18next';
import { ChatTimelineList } from '../components/ChatPanel/MessageList';
import { MarkdownMessageBody } from '../components/ChatPanel/MessageItem';
import { TeamMemberAvatar } from '../components/TeamMemberAvatar';
import { MarkdownIncludeMathMLContext } from '../components/MarkdownRenderer';
import { getMemberDisplayName } from '../components/teamArea/shared';
import { formatTeamEventTime, parseTeamEventMessage, type ParsedTeamEvent } from '../components/ChatPanel/teamEventUtils';
import { isUserMember } from '../utils/teamMemberAvatar';
import { parseHistoryJsonFileToTimelinePreview } from './historyRestore';
import { parseTeamHistoryPanelRecords } from './teamHistoryPanelRestore';
import { isA2UIClientEventContent } from './a2ui/a2uiContent';
import { getSvgNaturalHeight, getSvgNaturalWidth } from '../utils/svgDimensions';
import { generateUuidV4 } from '../utils/uuid';
import {
  SerializedShareImageClone,
  SHARE_IMAGE_KATEX_ATOM_SELECTOR,
  SHARE_IMAGE_PIXEL_RATIO,
  SHARE_IMAGE_WIDTH,
  cloneShareImageTreeToSerializedBlocks,
  getShareImageOutputDimensions,
  getShareImagePartOutputHeights,
  getShareImageTileSourceHeight,
  shouldIncludeShareImageCloneNode,
} from './shareImageRaster';
import { buildShareImageArtifact, type ShareImageExportArtifact } from './shareImageArchive';
import { ShareImagePngEncoder } from './shareImagePngEncoder';
import { PNG_SIGNATURE, buildPngChunk } from './streamingPng';
import i18n from '../i18n';
import './shareImageExport.css';

export interface ShareImageMetadata {
  title?: string;
  exported_at?: string;
  filename?: string;
}

export interface ShareImageSnapshot {
  session_id: string;
  metadata?: ShareImageMetadata;
  records: unknown[];
}

interface ShareImageDocumentProps {
  snapshot: ShareImageSnapshot | null;
}

interface GroupMessage {
  event: ParsedTeamEvent;
  timestampMs: number;
}

const OPENJIUWEN_WEBSITE_URL = 'https://openjiuwen.com';
const JIUWENSWARM_REPO_URL = 'https://gitcode.com/openJiuwen/jiuwenswarm';
const TRANSPARENT_IMAGE_DATA_URL = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';

function touchShareImageExportHeartbeat(): void {
  const runnerWindow = window as Window & {
    __SHARE_IMAGE_EXPORT_STATE?: { status?: string; heartbeat?: number };
  };
  const state = runnerWindow.__SHARE_IMAGE_EXPORT_STATE;
  if (state?.status !== 'rendering') return;
  runnerWindow.__SHARE_IMAGE_EXPORT_STATE = {
    ...state,
    heartbeat: (state.heartbeat ?? 0) + 1,
  };
}

function yieldToBrowser(): Promise<void> {
  touchShareImageExportHeartbeat();
  return new Promise(resolve => setTimeout(resolve, 0));
}

const shareImageStyleProperties: string[] = [];
const shareImageStylePropertySet = new Set<string>();

function addShareImageStyleProperty(property: string): void {
  if (property.startsWith('--')) return;
  if (property === 'all') {
    throw new Error('share_image_all_style_unsupported');
  }
  if (!shareImageStylePropertySet.has(property)) {
    shareImageStylePropertySet.add(property);
    shareImageStyleProperties.push(property);
  }
}

function splitShareImageSelectorList(selector: string): string[] {
  const selectors: string[] = [];
  let start = 0;
  let depth = 0;
  let quote = '';
  let escaped = false;
  for (let index = 0; index < selector.length; index++) {
    const character = selector[index];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (character === '\\') {
      escaped = true;
      continue;
    }
    if (quote) {
      if (character === quote) quote = '';
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
    } else if (character === '(' || character === '[') {
      depth++;
    } else if (character === ')' || character === ']') {
      depth--;
      if (depth < 0) throw new Error('share_image_style_selector_unsupported');
    } else if (character === ',' && depth === 0) {
      selectors.push(selector.slice(start, index).trim());
      start = index + 1;
    }
  }
  if (depth !== 0 || quote || escaped) {
    throw new Error('share_image_style_selector_unsupported');
  }
  selectors.push(selector.slice(start).trim());
  return selectors;
}

function shareImageSelectorMatchesSubtree(node: HTMLElement, selector: string): boolean {
  const matchableSelectors = splitShareImageSelectorList(selector).flatMap(part => {
    const withoutClonedPseudo = part.replace(/::(?:before|after)\b/g, '').trim();
    if (withoutClonedPseudo.length === 0 && /::(?:before|after)\b/.test(part)) return ['*'];
    return withoutClonedPseudo.includes('::') || withoutClonedPseudo.length === 0 ? [] : [withoutClonedPseudo];
  });
  if (matchableSelectors.length === 0) return false;
  const matchableSelector = matchableSelectors.join(', ');
  let current: Element | null = node;
  try {
    while (current) {
      if (current.matches(matchableSelector)) return true;
      current = current.parentElement;
    }
    return node.querySelector(matchableSelector) !== null;
  } catch {
    throw new Error('share_image_style_selector_unsupported');
  }
}

interface ShareImageAuthorStyleRule {
  selector: string;
  properties: string[];
}

interface ShareImageAuthorStyles {
  rules: ShareImageAuthorStyleRule[];
  unconditionalProperties: string[];
}

function getDeclaredStyleProperties(style: CSSStyleDeclaration): string[] {
  return Array.from({ length: style.length }, (_, index) => style.item(index));
}

function collectShareImageAuthorStyleRule(rule: CSSRule, result: ShareImageAuthorStyles): void {
  if (rule.type === CSSRule.STYLE_RULE) {
    const styleRule = rule as CSSStyleRule;
    result.rules.push({ selector: styleRule.selectorText, properties: getDeclaredStyleProperties(styleRule.style) });
  } else if (rule.type === CSSRule.KEYFRAME_RULE) {
    result.unconditionalProperties.push(...getDeclaredStyleProperties((rule as CSSKeyframeRule).style));
  }
  if (rule.type === CSSRule.MEDIA_RULE && !window.matchMedia((rule as CSSMediaRule).conditionText).matches) {
    return;
  }
  const nestedRules = (rule as CSSRule & { cssRules?: CSSRuleList }).cssRules;
  if (nestedRules) {
    for (const nestedRule of Array.from(nestedRules)) {
      collectShareImageAuthorStyleRule(nestedRule, result);
    }
  }
}

function prepareShareImageAuthorStyles(): ShareImageAuthorStyles {
  const result: ShareImageAuthorStyles = { rules: [], unconditionalProperties: [] };
  for (const stylesheet of Array.from(document.styleSheets)) {
    let rules: CSSRuleList;
    try {
      rules = stylesheet.cssRules;
    } catch {
      throw new Error('share_image_stylesheet_access_failed');
    }
    for (const rule of Array.from(rules)) {
      collectShareImageAuthorStyleRule(rule, result);
    }
  }
  return result;
}

function setShareImageStylePropertiesForNode(node: HTMLElement, authorStyles: ShareImageAuthorStyles): void {
  shareImageStyleProperties.length = 0;
  shareImageStylePropertySet.clear();
  for (const property of authorStyles.unconditionalProperties) {
    addShareImageStyleProperty(property);
  }
  for (const rule of authorStyles.rules) {
    if (!shareImageSelectorMatchesSubtree(node, rule.selector)) continue;
    for (const property of rule.properties) {
      addShareImageStyleProperty(property);
    }
  }
  let ancestor: Node | null = node;
  while (ancestor) {
    collectShareImageInlineStyleProperties(ancestor);
    ancestor = ancestor.parentNode;
  }
}

function collectShareImageInlineStyleProperties(node: Node): void {
  if (!(node instanceof HTMLElement) && !(node instanceof SVGElement)) return;
  for (let index = 0; index < node.style.length; index++) {
    addShareImageStyleProperty(node.style.item(index));
  }
}

async function waitForShareImageDocumentRendering(node: HTMLElement): Promise<void> {
  const hasPendingTimeline = (): boolean =>
    node.querySelector('[data-share-image-render-state="pending"]') !== null;
  if (!hasPendingTimeline()) return;

  await new Promise<void>((resolve, reject) => {
    const observer = new MutationObserver(() => {
      try {
        // Force each committed semantic item through layout before React adds
        // the next one, bounding a single layout task without approximating
        // any element height or changing the final document topology.
        void node.scrollHeight;
        touchShareImageExportHeartbeat();
        if (!hasPendingTimeline()) {
          observer.disconnect();
          resolve();
        }
      } catch (error) {
        observer.disconnect();
        reject(error);
      }
    });
    observer.observe(node, { attributes: true, childList: true, subtree: true });
  });
}

class ShareImageComputedStyleDictionary {
  private readonly stylesByHash = new Map<string, Array<{ token: string; styleText: string }>>();
  private readonly styleByToken = new Map<string, string>();

  constructor(private readonly markerAttribute: string) {}

  private registerStyle(styleText: string): string {
    let firstHash = 0x811c9dc5;
    let secondHash = 0x9e3779b9;
    for (let index = 0; index < styleText.length; index++) {
      const code = styleText.charCodeAt(index);
      firstHash = Math.imul(firstHash ^ code, 0x01000193);
      secondHash = Math.imul(secondHash ^ code, 0x85ebca6b);
    }
    const hash = `${firstHash >>> 0}:${secondHash >>> 0}:${styleText.length}`;
    const bucket = this.stylesByHash.get(hash) ?? [];
    const existing = bucket.find(entry => entry.styleText === styleText);
    if (existing) {
      return existing.token;
    }
    const token = (this.styleByToken.size + 1).toString(36);
    bucket.push({ token, styleText });
    this.stylesByHash.set(hash, bucket);
    this.styleByToken.set(token, styleText);
    return token;
  }

  async compactClone(clone: HTMLElement, yieldControl: () => Promise<void>): Promise<void> {
    const entries: Array<{ element: HTMLElement; token: string; yieldAfter: boolean }> = [];
    const elements = [clone, ...clone.querySelectorAll<HTMLElement>('*')];
    for (const element of elements) {
      const yieldAfter = element.matches(SHARE_IMAGE_KATEX_ATOM_SELECTOR);
      if (
        element.namespaceURI === 'http://www.w3.org/1999/xhtml' &&
        !(element instanceof HTMLStyleElement) &&
        element.style.length > 0
      ) {
        const properties = Array.from({ length: element.style.length }, (_, index) => element.style.item(index));
        for (const property of properties) {
          if (property.startsWith('--')) {
            element.style.removeProperty(property);
          } else if (element.style.getPropertyValue(property).includes('var(')) {
            throw new Error('share_image_unresolved_style_variable');
          }
        }
        if (element.style.length > 0) {
          entries.push({ element, token: this.registerStyle(element.style.cssText), yieldAfter });
        }
      }
      if (yieldAfter) {
        await yieldControl();
      }
    }
    for (const entry of entries) {
      entry.element.setAttribute(this.markerAttribute, entry.yieldAfter ? `${entry.token}:y` : entry.token);
      entry.element.removeAttribute('style');
    }
  }

  private createStylesheet(tokens: Iterable<string>): string {
    const rules: string[] = [];
    for (const token of tokens) {
      const styleText = this.styleByToken.get(token);
      if (styleText == null) {
        throw new Error('share_image_clone_structure_mismatch');
      }
      rules.push(`[${this.markerAttribute}="${token}"]{${styleText}}`);
    }
    const stylesheet = rules.join('');
    if (stylesheet.includes(']]>')) {
      throw new Error('share_image_style_rule_invalid');
    }
    return stylesheet;
  }

  async prepareTile(markup: string, yieldControl: () => Promise<void>): Promise<PreparedShareImageTile> {
    const markerPrefix = ` ${this.markerAttribute}="`;
    const output: string[] = [];
    const usedStyleTokens = new Set<string>();
    let cursor = 0;

    while (cursor < markup.length) {
      const tagStart = markup.indexOf('<', cursor);
      if (tagStart < 0) {
        output.push(markup.slice(cursor));
        break;
      }
      output.push(markup.slice(cursor, tagStart));

      let tagEnd: number;
      if (markup.startsWith('<!--', tagStart)) {
        const commentEnd = markup.indexOf('-->', tagStart + 4);
        if (commentEnd < 0) throw new Error('share_image_clone_structure_mismatch');
        tagEnd = commentEnd + 2;
      } else if (markup.startsWith('<![CDATA[', tagStart)) {
        const cdataEnd = markup.indexOf(']]>', tagStart + 9);
        if (cdataEnd < 0) throw new Error('share_image_clone_structure_mismatch');
        tagEnd = cdataEnd + 2;
      } else {
        tagEnd = markup.indexOf('>', tagStart + 1);
        if (tagEnd < 0) throw new Error('share_image_clone_structure_mismatch');
      }

      let tagMarkup = markup.slice(tagStart, tagEnd + 1);
      const markerStart = tagMarkup.indexOf(markerPrefix);
      if (markerStart >= 0) {
        const tokenStart = markerStart + markerPrefix.length;
        const tokenEnd = tagMarkup.indexOf('"', tokenStart);
        if (tokenEnd < 0 || tagMarkup.indexOf(markerPrefix, tokenEnd + 1) >= 0) {
          throw new Error('share_image_clone_structure_mismatch');
        }
        const markerValue = tagMarkup.slice(tokenStart, tokenEnd);
        const yieldAfter = markerValue.endsWith(':y');
        const token = yieldAfter ? markerValue.slice(0, -2) : markerValue;
        if (!this.styleByToken.has(token)) {
          throw new Error('share_image_clone_structure_mismatch');
        }
        usedStyleTokens.add(token);
        if (yieldAfter) {
          tagMarkup = `${tagMarkup.slice(0, tokenStart)}${token}${tagMarkup.slice(tokenEnd)}`;
        }
        output.push(tagMarkup);
        cursor = tagEnd + 1;
        if (yieldAfter) await yieldControl();
        continue;
      }

      output.push(tagMarkup);
      cursor = tagEnd + 1;
    }

    return {
      markup: output.join(''),
      styleCss: this.createStylesheet(usedStyleTokens),
    };
  }
}

interface PreparedShareImageTile {
  markup: string;
  styleCss: string;
}

class PreparedShareImageClone {
  constructor(
    private readonly clone: SerializedShareImageClone,
    private readonly computedStyles: ShareImageComputedStyleDictionary,
  ) {}

  async prepareTile(sourceY: number, sourceHeight: number): Promise<PreparedShareImageTile> {
    const compactMarkup = this.clone.prepareTile(sourceY, sourceHeight);
    return this.computedStyles.prepareTile(compactMarkup, yieldToBrowser);
  }

  dispose(): void {
    this.clone.dispose();
  }
}

async function prepareShareImageClone(
  node: HTMLElement,
  options: HtmlToImageOptions,
): Promise<PreparedShareImageClone> {
  const usedFontFamilies = new Set<string>();
  let styleMarkerAttribute = 'data-jss';
  while (node.hasAttribute(styleMarkerAttribute) || node.querySelector(`[${styleMarkerAttribute}]`)) {
    styleMarkerAttribute += 'x';
  }
  const computedStyles = new ShareImageComputedStyleDictionary(styleMarkerAttribute);
  const collectFontFamilies = (clone: HTMLElement): void => {
    const elements = [clone, ...clone.querySelectorAll<HTMLElement>('*')];
    for (const element of elements) {
      if (element.closest('.katex-html')) continue;
      for (const family of element.style.fontFamily.split(',')) {
        const normalized = family.trim().replace(/["']/g, '');
        if (normalized) {
          usedFontFamilies.add(normalized);
        }
      }
    }
  };
  const authorStyles = prepareShareImageAuthorStyles();
  setShareImageStylePropertiesForNode(node, authorStyles);
  const cloneOptions: HtmlToImageOptions = {
    ...options,
    includeStyleProperties: shareImageStyleProperties,
  };
  const includeContentNode = (candidate: HTMLElement): boolean => {
    collectShareImageInlineStyleProperties(candidate);
    return shouldIncludeShareImageCloneNode(candidate) && (!cloneOptions.filter || cloneOptions.filter(candidate));
  };
  const clone = await cloneShareImageTreeToSerializedBlocks(
    node,
    async (source, excludedBlocks) => {
      collectShareImageInlineStyleProperties(source);
      const inclusionByNode = new WeakMap<Node, boolean>();
      const isIncluded = (candidate: Node): boolean => {
        const cached = inclusionByNode.get(candidate);
        if (cached != null) return cached;
        const included =
          !excludedBlocks.has(candidate as HTMLElement) && includeContentNode(candidate as HTMLElement);
        inclusionByNode.set(candidate, included);
        return included;
      };
      const clonedNode = await cloneHtmlNode(
        source,
        {
          ...cloneOptions,
          filter: isIncluded,
        },
        true,
      );
      if (!(clonedNode instanceof HTMLElement)) {
        throw new Error('share_image_clone_failed');
      }
      return { clone: clonedNode, isIncluded };
    },
    async (clone, isRoot) => {
      const formulaHtmlRoots = [
        ...(clone.matches('.katex-html') ? [clone] : []),
        ...clone.querySelectorAll<HTMLElement>('.katex-html'),
      ];
      for (const formulaHtml of formulaHtmlRoots) {
        const walker = formulaHtml.ownerDocument.createTreeWalker(formulaHtml, NodeFilter.SHOW_TEXT);
        let current = walker.nextNode();
        while (current) {
          if ((current as Text).data.length > 0) {
            const parent = current.parentElement;
            if (!parent) throw new Error('share_image_katex_raster_structure_mismatch');
            parent.style.setProperty('color', 'transparent', 'important');
            parent.style.setProperty('-webkit-text-fill-color', 'transparent', 'important');
          }
          current = walker.nextNode();
        }
      }
      collectFontFamilies(clone);
      await embedImages(clone, cloneOptions);
      if (isRoot) {
        let fontOptions = cloneOptions;
        if (cloneOptions.fontEmbedCSS == null && !cloneOptions.skipFonts) {
          const fontProbe = node.ownerDocument.createElement('div');
          const families = [...usedFontFamilies];
          if (families.length > 0) {
            fontProbe.style.fontFamily = families[0];
            for (const family of families.slice(1)) {
              const familyProbe = node.ownerDocument.createElement('span');
              familyProbe.style.fontFamily = family;
              fontProbe.appendChild(familyProbe);
            }
          }
          const fontEmbedCSS = families.length > 0 ? await getWebFontCSS(fontProbe, cloneOptions) : '';
          if (fontEmbedCSS.includes(']]>')) {
            throw new Error('share_image_font_rule_invalid');
          }
          fontOptions = {
            ...cloneOptions,
            fontEmbedCSS,
          };
        }
        await embedWebFonts(clone, fontOptions);
        applyStyle(clone, cloneOptions);
        clone.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
      }
      await computedStyles.compactClone(clone, yieldToBrowser);
    },
    yieldToBrowser,
  );
  return new PreparedShareImageClone(clone, computedStyles);
}

async function createShareImageMarkup(
  preparedClone: PreparedShareImageClone,
  sourceY: number,
  sourceHeight: number,
): Promise<PreparedShareImageTile> {
  return preparedClone.prepareTile(sourceY, sourceHeight);
}

async function loadShareImageSvg(image: HTMLImageElement, svg: string): Promise<void> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    const cleanup = () => {
      reader.onload = null;
      reader.onerror = null;
      reader.onabort = null;
    };
    reader.onload = () => {
      if (typeof reader.result === 'string') {
        const result = reader.result;
        cleanup();
        resolve(result);
      } else {
        cleanup();
        reject(new Error('share_image_svg_data_url_failed'));
      }
    };
    reader.onerror = () => {
      cleanup();
      reject(new Error('share_image_svg_data_url_failed'));
    };
    reader.onabort = () => {
      cleanup();
      reject(new Error('share_image_svg_data_url_failed'));
    };
    reader.readAsDataURL(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }));
  });
  await new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      image.onload = null;
      image.onerror = null;
    };
    image.onload = () => {
      cleanup();
      resolve();
    };
    image.onerror = () => {
      cleanup();
      reject(new Error('share_image_svg_decode_failed'));
    };
    image.src = dataUrl;
  });
}

function createShareImageTileSvg(tile: PreparedShareImageTile, width: number, sourceY: number, sourceHeight: number): string {
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${sourceHeight}" viewBox="0 0 ${width} ${sourceHeight}">`,
    `<foreignObject x="0" y="0" width="100%" height="100%" externalResourcesRequired="true">`,
    `<div xmlns="http://www.w3.org/1999/xhtml" style="position:relative;width:${width}px;height:${sourceHeight}px;overflow:hidden">`,
    `<style type="text/css"><![CDATA[${tile.styleCss}]]></style>`,
    `<div style="width:${width}px;transform:translateY(${-sourceY}px);transform-origin:top left">`,
    tile.markup,
    '</div></div></foreignObject></svg>',
  ].join('');
}

type ShareImagePngAppendResult = { error: unknown } | null;

function trackShareImagePngAppend(append: Promise<void>): Promise<ShareImagePngAppendResult> {
  return append.then(
    () => null,
    error => ({ error }),
  );
}

async function waitForShareImagePngAppend(pending: Promise<ShareImagePngAppendResult> | null): Promise<void> {
  if (!pending) return;
  const result = await pending;
  if (result) throw result.error;
}

async function rasterizeShareImage(
  node: HTMLElement,
  options: HtmlToImageOptions,
  width: number,
  height: number,
  backgroundColor: string,
  katexOverlays: ShareImageKaTeXOverlay[],
): Promise<Blob[]> {
  if (width !== SHARE_IMAGE_WIDTH) {
    throw new Error('share_image_invalid_width');
  }
  const [outputWidth] = getShareImageOutputDimensions(height);
  const partOutputHeights = getShareImagePartOutputHeights(height);
  const tileSourceHeight = getShareImageTileSourceHeight();
  const parts: Blob[] = [];
  let partIndex = 0;
  let appendedPartRows = 0;
  let encoder: ShareImagePngEncoder | null = new ShareImagePngEncoder(outputWidth, partOutputHeights[0]);
  let preparedClone: PreparedShareImageClone | null = null;
  let pendingPngAppend: Promise<ShareImagePngAppendResult> | null = null;

  try {
    preparedClone = await prepareShareImageClone(node, options);
    const canvas = document.createElement('canvas');
    canvas.width = outputWidth;
    canvas.height = tileSourceHeight * SHARE_IMAGE_PIXEL_RATIO;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) {
      throw new Error('share_image_canvas_context_unavailable');
    }
    const image = new Image();

    try {
      for (let sourceY = 0; sourceY < height; sourceY += tileSourceHeight) {
        const sourceHeight = Math.min(tileSourceHeight, height - sourceY);
        const renderedHeight = sourceHeight * SHARE_IMAGE_PIXEL_RATIO;
        if (canvas.height !== renderedHeight) {
          canvas.height = renderedHeight;
        }
        context.fillStyle = backgroundColor;
        context.fillRect(0, 0, outputWidth, renderedHeight);

        const markup = await createShareImageMarkup(preparedClone, sourceY, sourceHeight);
        try {
          await loadShareImageSvg(image, createShareImageTileSvg(markup, width, sourceY, sourceHeight));
          context.drawImage(image, 0, 0, outputWidth, renderedHeight);
        } finally {
          image.onload = null;
          image.onerror = null;
          image.removeAttribute('src');
        }
        drawKaTeXFormulaOverlays(
          context,
          sourceY * SHARE_IMAGE_PIXEL_RATIO,
          renderedHeight,
          katexOverlays,
        );

        // Keep exactly one encoded tile in flight. SVG preparation and decode
        // for this tile can overlap PNG filtering/compression for the previous
        // tile, while waiting here prevents a second RGBA buffer from being
        // read back before the worker has released the first one.
        await waitForShareImagePngAppend(pendingPngAppend);
        pendingPngAppend = null;
        const rgba = context.getImageData(0, 0, outputWidth, renderedHeight).data;
        const rowBytes = outputWidth * 4;
        let tileRowOffset = 0;
        while (tileRowOffset < renderedHeight) {
          await waitForShareImagePngAppend(pendingPngAppend);
          pendingPngAppend = null;
          if (encoder === null) {
            throw new Error('share_image_part_encoder_missing');
          }
          const partHeight = partOutputHeights[partIndex];
          const rowCount = Math.min(renderedHeight - tileRowOffset, partHeight - appendedPartRows);
          const byteOffset = tileRowOffset * rowBytes;
          pendingPngAppend = trackShareImagePngAppend(encoder.appendRgbaRows(
            rgba.subarray(byteOffset, byteOffset + rowCount * rowBytes),
            rowCount,
          ));
          appendedPartRows += rowCount;
          tileRowOffset += rowCount;

          if (appendedPartRows === partHeight) {
            await waitForShareImagePngAppend(pendingPngAppend);
            pendingPngAppend = null;
            // Worker transfers detach ArrayBuffers, so each part must own its
            // metadata bytes instead of reusing the previous part's buffer.
            parts.push(await encoder.finish([buildAigcITextChunk()]));
            partIndex++;
            appendedPartRows = 0;
            encoder = partIndex < partOutputHeights.length
              ? new ShareImagePngEncoder(outputWidth, partOutputHeights[partIndex])
              : null;
          }
        }
        await nextFrame();
      }
      await waitForShareImagePngAppend(pendingPngAppend);
      pendingPngAppend = null;
      if (encoder !== null || parts.length !== partOutputHeights.length) {
        throw new Error('share_image_parts_incomplete');
      }
      return parts;
    } finally {
      image.onload = null;
      image.onerror = null;
      image.removeAttribute('src');
      canvas.width = 0;
      canvas.height = 0;
    }
  } catch (error) {
    if (encoder !== null) await encoder.abort(error);
    throw error;
  } finally {
    preparedClone?.dispose();
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Filter out A2UI client event messages from the message list.
 * These messages are internal interaction events and should not be included in exports.
 */
function filterA2UIClientEvents(messages: unknown[]): unknown[] {
  return messages.filter(msg => {
    if (!isRecord(msg)) return true;
    if (msg.role === 'user' && isA2UIClientEventContent(msg.content)) return false;
    return true;
  });
}

function normalizeMode(records: unknown[]): string {
  const modes = records
    .filter(isRecord)
    .map(record => (typeof record.mode === 'string' ? record.mode.trim().toLowerCase() : ''))
    .filter(Boolean);
  return modes.includes('team') ? 'team' : modes[0] || 'agent';
}

function readableDate(value?: string): string {
  if (!value) {
    return '';
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function collectGroupMessages(snapshot: ShareImageSnapshot): GroupMessage[] {
  const state = parseTeamHistoryPanelRecords(snapshot.records, snapshot.session_id);
  const items: GroupMessage[] = [];

  for (const message of state.messages) {
    const event = parseTeamEventMessage(message);
    if (!event || event.isLeaderToUser) {
      continue;
    }
    items.push({
      event,
      timestampMs: event.timestamp || Date.parse(message.timestamp) || 0,
    });
  }

  return items.sort((a, b) => a.timestampMs - b.timestampMs);
}

function GroupChatMessage({ item }: { item: GroupMessage }) {
  const { t } = useTranslation();
  const { event } = item;
  const isUser = isUserMember(event.fromMember);
  const displayName = getMemberDisplayName(event.fromMember);
  const timeText = formatTeamEventTime(event.timestamp);

  return (
    <article className={`share-image-group-message ${isUser ? 'is-user' : ''}`}>
      {!isUser && <TeamMemberAvatar member={event.fromMember} className="share-image-group-message__avatar" />}
      <div className="share-image-group-message__main">
        <div className="share-image-group-message__meta">
          <span className="share-image-group-message__member">{displayName}</span>
          {timeText && <span className="share-image-group-message__time">{timeText}</span>}
        </div>
        <div className="share-image-group-message__bubble">
          {event.isP2P && event.toMember && <span className="share-image-group-message__chip">@{getMemberDisplayName(event.toMember)}</span>}
          {event.isBroadcast && <span className="share-image-group-message__chip">{t('share.everyone')}</span>}
          <MarkdownMessageBody content={event.content} className="share-image-group-message__body" />
        </div>
      </div>
      {isUser && <TeamMemberAvatar member={event.fromMember} className="share-image-group-message__avatar" />}
    </article>
  );
}

export const ShareImageDocument = forwardRef<HTMLDivElement, ShareImageDocumentProps>(function ShareImageDocument({ snapshot }, ref) {
  const { t } = useTranslation();
  const data = useMemo(() => {
    if (!snapshot) {
      return null;
    }
    const preview = parseHistoryJsonFileToTimelinePreview(snapshot.records, snapshot.session_id);
    // Filter out A2UI client event messages from exports
    const filteredMessages = filterA2UIClientEvents(preview.messages) as typeof preview.messages;
    return {
      mode: normalizeMode(snapshot.records),
      messages: filteredMessages,
      executions: preview.executions,
      reasoningSegments: preview.reasoningSegments,
      groupMessages: collectGroupMessages(snapshot),
    };
  }, [snapshot]);

  if (!snapshot || !data) {
    return <div ref={ref} className="share-image-document" />;
  }

  const title = snapshot.metadata?.title?.trim() || snapshot.session_id;
  const exportedAt = readableDate(snapshot.metadata?.exported_at);
  const hasConversation = data.messages.length > 0;
  const isTeamMode = data.mode === 'team';
  const hasGroupMessages = data.groupMessages.length > 0;
  const aiNotice = t('share.aiNotice');

  return (
    <MarkdownIncludeMathMLContext.Provider value={false}>
      <div ref={ref} className="share-image-document">
      <header className="share-image-header">
        <div className="share-image-masthead">
          <div className="share-image-brand">
            <img src="/logo.svg" alt="" className="share-image-brand__logo" />
            <div className="share-image-brand__name">WorkSwarm</div>
          </div>
        </div>
      </header>

      <main className="share-image-content">
        <div className="share-image-content-header">
          <h1>{title}</h1>
          <div className="share-image-meta">
            <span>{snapshot.session_id}</span>
            {exportedAt && <span>{exportedAt}</span>}
          </div>
        </div>

        <section className="share-image-section">
          <div className="share-image-section__label">{t('share.mainConversation')}</div>
          {hasConversation ? (
            <ChatTimelineList
              messages={data.messages}
              executions={data.executions}
              reasoningSegments={data.reasoningSegments}
              staticTimeline
              incrementalStaticRendering
              mode={data.mode}
              disableA2UIInteraction={true}
            />
          ) : (
            <div className="share-image-empty">{t('share.noMainConversation')}</div>
          )}
        </section>

        {isTeamMode && (
          <section className="share-image-section share-image-section--group">
            <div className="share-image-section__label">{t('share.groupChat')}</div>
            {hasGroupMessages ? (
              <div className="share-image-group-list">
                {data.groupMessages.map(item => (
                  <GroupChatMessage key={item.event.messageId} item={item} />
                ))}
              </div>
            ) : (
              <div className="share-image-empty">{t('share.noGroupChat')}</div>
            )}
          </section>
        )}
      </main>

      <footer className="share-image-footer">
        <div className="share-image-footer__note">{aiNotice}</div>
        <div className="share-image-links">
          <div className="share-image-link">
            <span>{t('share.website', { url: OPENJIUWEN_WEBSITE_URL })}</span>
          </div>
          <div className="share-image-link-divider" />
          <div className="share-image-link">
            <span>{t('share.repository', { url: JIUWENSWARM_REPO_URL })}</span>
          </div>
        </div>
      </footer>
      </div>
    </MarkdownIncludeMathMLContext.Provider>
  );
});

function nextFrame(): Promise<void> {
  touchShareImageExportHeartbeat();
  return new Promise(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
  });
}

interface ImageSnapshot {
  image: HTMLImageElement;
  src: string | null;
  srcset: string | null;
  sizes: string | null;
}

interface KaTeXTextRun {
  text: string;
  x: number;
  baseline: number;
  font: string;
  letterSpacing: string;
  wordSpacing: string;
  direction: CanvasDirection;
  fillStyle: string;
  opacity: number;
  actualLeft: number;
  actualRight: number;
  actualAscent: number;
  actualDescent: number;
}

interface ShareImageKaTeXOverlay {
  runs: KaTeXTextRun[];
  rootOffsetX: number;
  rootOffsetY: number;
  topPixel: number;
  bottomPixel: number;
}

interface PreparedKaTeXFormulaOverlays {
  overlays: ShareImageKaTeXOverlay[];
  restore: () => void;
}

type SpacingCanvasContext = CanvasRenderingContext2D & {
  letterSpacing?: string;
  wordSpacing?: string;
};

function parseFiniteOpacity(value: string): number {
  const opacity = Number(value);
  if (!Number.isFinite(opacity) || opacity < 0 || opacity > 1) {
    throw new Error('share_image_katex_raster_style_unsupported');
  }
  return opacity;
}

function getKaTeXTextOpacity(parent: HTMLElement, root: HTMLElement): number {
  let opacity = 1;
  let current: HTMLElement | null = parent;
  while (current && current !== root) {
    const computedStyle = window.getComputedStyle(current);
    if (computedStyle.transform !== 'none' || computedStyle.zoom !== '1') {
      throw new Error('share_image_katex_raster_style_unsupported');
    }
    opacity *= parseFiniteOpacity(computedStyle.opacity);
    current = current.parentElement;
  }
  if (current !== root) {
    throw new Error('share_image_katex_raster_structure_mismatch');
  }
  return opacity;
}

function getTextBaseline(textNode: Text): number {
  const parent = textNode.parentElement;
  if (!parent) {
    throw new Error('share_image_katex_raster_structure_mismatch');
  }
  const marker = textNode.ownerDocument.createElement('span');
  marker.setAttribute('aria-hidden', 'true');
  marker.style.cssText =
    'display:inline-block;width:0;height:0;padding:0;margin:0;border:0;vertical-align:baseline;line-height:0;';
  textNode.after(marker);
  try {
    return marker.getBoundingClientRect().top;
  } finally {
    marker.remove();
  }
}

function setKaTeXCanvasTextStyle(
  context: SpacingCanvasContext,
  computedStyle: CSSStyleDeclaration,
  text: string,
): TextMetrics {
  if (
    (computedStyle.fontStretch !== '100%' && computedStyle.fontStretch !== 'normal') ||
    computedStyle.fontVariant !== 'normal' ||
    computedStyle.writingMode !== 'horizontal-tb' ||
    computedStyle.textShadow !== 'none' ||
    computedStyle.getPropertyValue('-webkit-text-stroke-width') !== '0px'
  ) {
    throw new Error('share_image_katex_raster_style_unsupported');
  }
  if (!('letterSpacing' in context) || !('wordSpacing' in context)) {
    throw new Error('share_image_katex_raster_canvas_unsupported');
  }
  context.font = computedStyle.font;
  context.letterSpacing = computedStyle.letterSpacing;
  context.wordSpacing = computedStyle.wordSpacing;
  context.direction = computedStyle.direction === 'rtl' ? 'rtl' : 'ltr';
  context.textAlign = 'start';
  context.textBaseline = 'alphabetic';
  const primaryFamily = computedStyle.fontFamily.split(',')[0]?.trim();
  const normalizedPrimaryFamily = primaryFamily?.replace(/^(['"])(.*)\1$/, '$2');
  if (normalizedPrimaryFamily?.startsWith('KaTeX_')) {
    const exactFont = `${computedStyle.fontStyle} ${computedStyle.fontWeight} ${computedStyle.fontSize} ${primaryFamily}`;
    const matchingFaceLoaded = Array.from(document.fonts).some(
      face =>
        face.family.replace(/^(['"])(.*)\1$/, '$2') === normalizedPrimaryFamily && face.status === 'loaded',
    );
    if (!matchingFaceLoaded || !document.fonts.check(exactFont, text)) {
      throw new Error('share_image_katex_font_unavailable');
    }
  }
  return context.measureText(text);
}

function getKaTeXTextRuns(root: HTMLElement, context: SpacingCanvasContext): KaTeXTextRun[] {
  const html = root.querySelector<HTMLElement>('.katex-html');
  if (!html) {
    throw new Error('share_image_katex_raster_structure_mismatch');
  }
  const rootRect = root.getBoundingClientRect();
  const walker = root.ownerDocument.createTreeWalker(html, NodeFilter.SHOW_TEXT);
  const runs: KaTeXTextRun[] = [];
  let current = walker.nextNode();
  while (current) {
    const textNode = current as Text;
    const text = textNode.data;
    const parent = textNode.parentElement;
    if (!parent || parent.closest('.katex') !== root) {
      throw new Error('share_image_katex_raster_structure_mismatch');
    }
    if (text.length > 0) {
      const range = root.ownerDocument.createRange();
      range.selectNodeContents(textNode);
      const rects = Array.from(range.getClientRects()).filter(rect => rect.width > 0 && rect.height > 0);
      if (rects.length > 1) {
        throw new Error('share_image_katex_raster_multiline_text');
      }
      if (rects.length === 1) {
        const rect = rects[0];
          const computedStyle = window.getComputedStyle(parent);
          if (computedStyle.visibility !== 'hidden' && computedStyle.display !== 'none') {
            const metrics = setKaTeXCanvasTextStyle(context, computedStyle, text);
            if (Math.abs(metrics.width - rect.width) > 1 / 64) {
              throw new Error('share_image_katex_raster_metrics_mismatch');
            }
          if (
            metrics.actualBoundingBoxLeft !== 0 ||
            metrics.actualBoundingBoxRight !== 0 ||
            metrics.actualBoundingBoxAscent !== 0 ||
            metrics.actualBoundingBoxDescent !== 0
          ) {
            runs.push({
              text,
              x: (computedStyle.direction === 'rtl' ? rect.right : rect.left) - rootRect.left,
              baseline: getTextBaseline(textNode) - rootRect.top,
              font: computedStyle.font,
              letterSpacing: computedStyle.letterSpacing,
              wordSpacing: computedStyle.wordSpacing,
              direction: computedStyle.direction === 'rtl' ? 'rtl' : 'ltr',
              fillStyle:
                computedStyle.getPropertyValue('-webkit-text-fill-color').trim() || computedStyle.color,
              opacity: getKaTeXTextOpacity(parent, root),
              actualLeft: metrics.actualBoundingBoxLeft,
              actualRight: metrics.actualBoundingBoxRight,
              actualAscent: metrics.actualBoundingBoxAscent,
              actualDescent: metrics.actualBoundingBoxDescent,
            });
          }
        }
      }
      range.detach();
    }
    current = walker.nextNode();
  }
  return runs;
}

function createKaTeXFormulaOverlay(
  root: HTMLElement,
  documentRect: DOMRect,
  measurementContext: SpacingCanvasContext,
): ShareImageKaTeXOverlay | null {
  const runs = getKaTeXTextRuns(root, measurementContext);
  if (runs.length === 0) {
    return null;
  }

  const rootRect = root.getBoundingClientRect();
  let minY = 0;
  let maxY = rootRect.height;
  for (const run of runs) {
    minY = Math.min(minY, run.baseline - run.actualAscent);
    maxY = Math.max(maxY, run.baseline + run.actualDescent);
  }

  const rootOffsetX = rootRect.left - documentRect.left;
  const rootOffsetY = rootRect.top - documentRect.top;
  const topPixel = Math.floor((rootOffsetY + minY) * SHARE_IMAGE_PIXEL_RATIO);
  const bottomPixel = Math.ceil((rootOffsetY + maxY) * SHARE_IMAGE_PIXEL_RATIO);
  if (bottomPixel <= topPixel) {
    throw new Error('share_image_katex_raster_structure_mismatch');
  }
  return {
    runs,
    rootOffsetX,
    rootOffsetY,
    topPixel,
    bottomPixel,
  };
}

async function prepareKaTeXFormulaOverlays(node: HTMLElement): Promise<PreparedKaTeXFormulaOverlays> {
  const roots = Array.from(node.querySelectorAll<HTMLElement>('.katex'));
  if (roots.length === 0) {
    return { overlays: [], restore: () => {} };
  }
  const documentRect = node.getBoundingClientRect();
  const measurementCanvas = document.createElement('canvas');
  const measurementContext = measurementCanvas.getContext('2d');
  if (!measurementContext) {
    throw new Error('share_image_canvas_context_unavailable');
  }
  const overlays: ShareImageKaTeXOverlay[] = [];
  for (const root of roots) {
    const rootStyle = window.getComputedStyle(root);
    if (rootStyle.position === 'static' || rootStyle.transform !== 'none' || rootStyle.zoom !== '1') {
      throw new Error('share_image_katex_raster_style_unsupported');
    }
    const overlay = createKaTeXFormulaOverlay(root, documentRect, measurementContext);
    if (overlay) overlays.push(overlay);
    await yieldToBrowser();
  }
  return {
    overlays,
    restore: () => {},
  };
}

function drawKaTeXFormulaOverlays(
  context: SpacingCanvasContext,
  tileTopPixel: number,
  tileHeight: number,
  overlays: ShareImageKaTeXOverlay[],
): void {
  if (!('letterSpacing' in context) || !('wordSpacing' in context)) {
    throw new Error('share_image_katex_raster_canvas_unsupported');
  }
  const tileBottomPixel = tileTopPixel + tileHeight;
  for (const overlay of overlays) {
    if (overlay.bottomPixel <= tileTopPixel || overlay.topPixel >= tileBottomPixel) continue;
    context.save();
    context.setTransform(
      SHARE_IMAGE_PIXEL_RATIO,
      0,
      0,
      SHARE_IMAGE_PIXEL_RATIO,
      overlay.rootOffsetX * SHARE_IMAGE_PIXEL_RATIO,
      overlay.rootOffsetY * SHARE_IMAGE_PIXEL_RATIO - tileTopPixel,
    );
    for (const run of overlay.runs) {
      context.font = run.font;
      context.letterSpacing = run.letterSpacing;
      context.wordSpacing = run.wordSpacing;
      context.direction = run.direction;
      context.textAlign = 'start';
      context.textBaseline = 'alphabetic';
      context.fillStyle = run.fillStyle;
      context.globalAlpha = run.opacity;
      context.fillText(run.text, run.x, run.baseline);
    }
    context.restore();
  }
}

function replaceBrokenImageForExport(image: HTMLImageElement, snapshots: ImageSnapshot[]): void {
  snapshots.push({
    image,
    src: image.getAttribute('src'),
    srcset: image.getAttribute('srcset'),
    sizes: image.getAttribute('sizes'),
  });
  image.removeAttribute('srcset');
  image.removeAttribute('sizes');
  image.src = TRANSPARENT_IMAGE_DATA_URL;
}

async function waitForImage(image: HTMLImageElement): Promise<boolean> {
  if (image.complete) {
    return image.naturalWidth > 0;
  }
  if (typeof image.decode === 'function') {
    await image.decode();
    return image.naturalWidth > 0;
  }
  return new Promise<boolean>(resolve => {
    image.addEventListener('load', () => resolve(image.naturalWidth > 0), { once: true });
    image.addEventListener('error', () => resolve(false), { once: true });
  });
}

async function prepareImagesForExport(node: HTMLElement): Promise<() => void> {
  const images = Array.from(node.querySelectorAll('img'));
  const snapshots: ImageSnapshot[] = [];

  await Promise.all(
    images.map(async image => {
      try {
        if (await waitForImage(image)) {
          return;
        }
      } catch {
        // Ignore broken or undecodable images in share export. A2UI Image can
        // intentionally contain an invalid URL to demonstrate fallback UI.
      }

      replaceBrokenImageForExport(image, snapshots);
      try {
        await waitForImage(image);
      } catch {
        // The transparent data URL should decode, but keep export tolerant.
      }
    }),
  );

  return () => {
    for (const snapshot of snapshots) {
      const { image, src, srcset, sizes } = snapshot;
      if (src === null) image.removeAttribute('src');
      else image.setAttribute('src', src);
      if (srcset === null) image.removeAttribute('srcset');
      else image.setAttribute('srcset', srcset);
      if (sizes === null) image.removeAttribute('sizes');
      else image.setAttribute('sizes', sizes);
    }
  };
}

interface MermaidExportSnapshot {
  svg: SVGSVGElement;
  width: string | null;
  height: string | null;
  svgStyle: string;
  canvas: HTMLElement;
  canvasStyle: string;
  wrapper: HTMLElement;
  wrapperStyle: string;
}

/**
 * Flattens the interactive Mermaid viewport into normal-flow export markup.
 * The live viewer intentionally keeps a bounded canvas and transforms an
 * absolutely positioned wrapper. That transform is not part of the SVG's
 * layout, so leaving it in the share document lets tall diagrams paint over
 * the following message. Preserve the live rendered scale, fit wide diagrams
 * to the export column, and give the canvas the diagram's actual height.
 */
function fitMermaidDiagramsForExport(node: HTMLElement): () => void {
  const svgs = Array.from(node.querySelectorAll<SVGSVGElement>('.share-image-document .mermaid-canvas svg'));
  const snapshots: MermaidExportSnapshot[] = [];

  for (const svg of svgs) {
    const naturalWidth = getSvgNaturalWidth(svg);
    const naturalHeight = getSvgNaturalHeight(svg);
    if (naturalWidth <= 0 || naturalHeight <= 0) continue;

    const canvas = svg.closest<HTMLElement>('.mermaid-canvas');
    const wrapper = svg.closest<HTMLElement>('.mermaid-svg-wrapper') ?? svg.parentElement;
    if (!canvas || !wrapper) continue;

    const renderedWidth = svg.getBoundingClientRect().width || naturalWidth;
    const exportWidth = Math.min(renderedWidth, canvas.clientWidth || renderedWidth);
    const ratio = exportWidth / naturalWidth;
    const exportHeight = naturalHeight * ratio;
    snapshots.push({
      svg,
      width: svg.getAttribute('width'),
      height: svg.getAttribute('height'),
      svgStyle: svg.getAttribute('style') ?? '',
      canvas,
      canvasStyle: canvas.getAttribute('style') ?? '',
      wrapper,
      wrapperStyle: wrapper.getAttribute('style') ?? '',
    });

    svg.setAttribute('width', String(exportWidth));
    svg.setAttribute('height', String(exportHeight));
    svg.style.width = `${exportWidth}px`;
    svg.style.height = `${exportHeight}px`;
    svg.style.maxWidth = 'none';

    // Keep the live viewport's breathing room, but make the diagram part of
    // normal flow so the next message starts after the full SVG.
    canvas.style.height = `${exportHeight + 48}px`;
    wrapper.style.position = 'static';
    wrapper.style.left = 'auto';
    wrapper.style.top = 'auto';
    wrapper.style.width = '100%';
    wrapper.style.height = '100%';
    wrapper.style.display = 'flex';
    wrapper.style.alignItems = 'center';
    wrapper.style.justifyContent = 'center';
    wrapper.style.transform = 'none';
  }

  return () => {
    for (const snapshot of snapshots) {
      const { svg, width, height, svgStyle, canvas, canvasStyle, wrapper, wrapperStyle } = snapshot;
      if (width === null) svg.removeAttribute('width');
      else svg.setAttribute('width', width);
      if (height === null) svg.removeAttribute('height');
      else svg.setAttribute('height', height);
      if (svgStyle) svg.setAttribute('style', svgStyle);
      else svg.removeAttribute('style');
      if (canvasStyle) canvas.setAttribute('style', canvasStyle);
      else canvas.removeAttribute('style');
      if (wrapperStyle) wrapper.setAttribute('style', wrapperStyle);
      else wrapper.removeAttribute('style');
    }
  };
}

async function waitForMermaidDiagrams(node: HTMLElement): Promise<void> {
  function assertNoFailedDiagrams(): void {
    if (node.querySelector('[data-mermaid-status="error"]')) {
      throw new Error('share_image_mermaid_render_failed');
    }
  }

  function hasPendingDiagrams(): boolean {
    return node.querySelector('[data-mermaid-status="loading"]') !== null;
  }

  function allRenderedDiagramsHaveSvg(): boolean {
    return Array.from(node.querySelectorAll('[data-mermaid-status="rendered"]')).every(diagram => diagram.querySelector('svg'));
  }

  function isReady(): boolean {
    assertNoFailedDiagrams();
    return !hasPendingDiagrams() && allRenderedDiagramsHaveSvg();
  }

  if (isReady()) {
    return;
  }

  await new Promise<void>((resolve, reject) => {
    const observer = new MutationObserver(() => {
      try {
        if (isReady()) {
          observer.disconnect();
          resolve();
        }
      } catch (error) {
        observer.disconnect();
        reject(error);
      }
    });

    try {
      if (isReady()) {
        resolve();
        return;
      }
      observer.observe(node, { childList: true, subtree: true });
    } catch (error) {
      observer.disconnect();
      reject(error);
    }
  });
}

export async function exportShareImageNode(
  node: HTMLElement,
  filename = 'jiuwenswarm-share.png',
): Promise<ShareImageExportArtifact> {
  await waitForShareImageDocumentRendering(node);
  await document.fonts?.ready;
  await nextFrame();
  let restoreKaTeXFormulaOverlays = (): void => {};
  let katexOverlays: ShareImageKaTeXOverlay[] = [];
  let restoreImages = (): void => {};
  let restoreMermaidDiagrams = (): void => {};
  try {
    const preparedKaTeXFormulaOverlays = await prepareKaTeXFormulaOverlays(node);
    katexOverlays = preparedKaTeXFormulaOverlays.overlays;
    restoreKaTeXFormulaOverlays = preparedKaTeXFormulaOverlays.restore;
    restoreImages = await prepareImagesForExport(node);
    await waitForMermaidDiagrams(node);
    await nextFrame();

    // Flatten Mermaid's interactive viewport so tall diagrams cannot overlap
    // the following content. The export DOM must remain mounted until the SVG
    // markup is built.
    restoreMermaidDiagrams = fitMermaidDiagramsForExport(node);
    await nextFrame();

    const backgroundColor = window.getComputedStyle(node).backgroundColor;
    const height = node.scrollHeight;
    const options: HtmlToImageOptions = {
      cacheBust: true,
      width: SHARE_IMAGE_WIDTH,
      height,
      backgroundColor,
    };
    const pngParts = await rasterizeShareImage(
      node,
      options,
      SHARE_IMAGE_WIDTH,
      height,
      backgroundColor,
      katexOverlays,
    );
    return buildShareImageArtifact(pngParts, filename, touchShareImageExportHeartbeat);
  } finally {
    restoreMermaidDiagrams();
    restoreImages();
    restoreKaTeXFormulaOverlays();
  }
}

type ShareImageExportRunnerState = {
  status: 'loading_snapshot' | 'rendering' | 'ready' | 'error';
  heartbeat?: number;
  filename?: string;
  error?: string;
};

type ShareImageExportRunnerWindow = Window & {
  __SHARE_IMAGE_EXPORT_STATE?: ShareImageExportRunnerState;
  __DOWNLOAD_SHARE_IMAGE__?: () => void;
};

interface ShareImageExportJobSnapshot {
  filename?: string;
  locale?: string;
  snapshot?: ShareImageSnapshot;
}

function shareImageExportError(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'share_export_render_failed';
}

/** Dedicated entrypoint used only by the server-owned headless export browser. */
export function ShareImageExportRunner({ jobId }: { jobId: string }): JSX.Element {
  const [snapshot, setSnapshot] = useState<ShareImageSnapshot | null>(null);
  const filenameRef = useRef('jiuwenswarm-share.png');
  const documentRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const runnerWindow = window as ShareImageExportRunnerWindow;
    const abortController = new AbortController();
    runnerWindow.__SHARE_IMAGE_EXPORT_STATE = { status: 'loading_snapshot' };
    void (async () => {
      try {
        const response = await fetch(`/share-api/jobs/${encodeURIComponent(jobId)}/snapshot`, {
          cache: 'no-store',
          signal: abortController.signal,
        });
        if (!response.ok) {
          throw new Error(`share_export_snapshot_http_${response.status}`);
        }
        const payload = await response.json() as ShareImageExportJobSnapshot;
        if (!payload.snapshot) {
          throw new Error('share_export_snapshot_missing');
        }
        if (payload.locale) {
          await i18n.changeLanguage(payload.locale);
        }
        filenameRef.current = payload.filename || payload.snapshot.metadata?.filename || filenameRef.current;
        setSnapshot(payload.snapshot);
      } catch (error) {
        if (abortController.signal.aborted) return;
        runnerWindow.__SHARE_IMAGE_EXPORT_STATE = {
          status: 'error',
          error: shareImageExportError(error),
        };
      }
    })();
    return () => abortController.abort();
  }, [jobId]);

  useEffect(() => {
    if (!snapshot) return;
    const runnerWindow = window as ShareImageExportRunnerWindow;
    let blobUrl = '';
    let disposed = false;
    runnerWindow.__SHARE_IMAGE_EXPORT_STATE = { status: 'rendering', heartbeat: 0 };
    void (async () => {
      try {
        const node = documentRef.current;
        if (!node) throw new Error('share_image_node_missing');
        const artifact = await exportShareImageNode(node, filenameRef.current);
        if (disposed) return;
        blobUrl = URL.createObjectURL(artifact.blob);
        runnerWindow.__DOWNLOAD_SHARE_IMAGE__ = () => {
          const anchor = document.createElement('a');
          anchor.href = blobUrl;
          anchor.download = artifact.filename;
          anchor.style.display = 'none';
          document.body.appendChild(anchor);
          anchor.click();
          anchor.remove();
        };
        runnerWindow.__SHARE_IMAGE_EXPORT_STATE = { status: 'ready', filename: artifact.filename };
      } catch (error) {
        if (disposed) return;
        runnerWindow.__SHARE_IMAGE_EXPORT_STATE = {
          status: 'error',
          error: shareImageExportError(error),
        };
      }
    })();
    return () => {
      disposed = true;
      runnerWindow.__DOWNLOAD_SHARE_IMAGE__ = undefined;
      if (blobUrl) URL.revokeObjectURL(blobUrl);
    };
  }, [snapshot]);

  return <ShareImageDocument ref={documentRef} snapshot={snapshot} />;
}

const AIGC_TEXT_ENCODER = new TextEncoder();

const EMPTY_MD5 = '';

/**
 * Build the GB 45438-2025 implicit AIGC label as an XMP packet string. The
 * seven fields (standard Appendix E §c-§i) are placed both as attributes of
 * the `AIGC` namespace on rdf:Description and, redundantly, as an
 * `AIGC:{flat-json}` string inside a `<AIGC:AIGC>` element — readers that
 * key on either form can extract Label/ContentProducer/ProduceID/etc.
 *
 * ReservedCode1/2 store integrity/security info (§f/§i); kept non-empty
 * using the MD5 of empty input as a placeholder (the same convention
 * Alibaba's docs use), since some platforms reject empty reserved fields.
 */
function buildAigcLabel(): { xmp: string } {
  const producer = 'WorkSwarm';
  const produceId = generateUuidV4();
  const payload = {
    Label: '1',
    ContentProducer: producer,
    ProduceID: produceId,
    ReservedCode1: EMPTY_MD5,
    ContentPropagator: producer,
    PropagateID: produceId,
    ReservedCode2: EMPTY_MD5,
  };
  const json = `AIGC:${JSON.stringify(payload)}`;
  const xmp = [
    '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>',
    '<x:xmpmeta xmlns:x="adobe:ns:meta/">',
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">',
    // rdf:about and the xmlns:AIGC declaration MUST stay on one line —
    // splitting them across lines breaks detection platforms whose XMP
    // parser fails to bind the AIGC namespace, dropping every AIGC:* attr.
    '<rdf:Description rdf:about="" xmlns:AIGC="urn:gb-45438-2025:aigc"',
    ` AIGC:Label="1"`,
    ` AIGC:ContentProducer="${producer}"`,
    ` AIGC:ProduceID="${produceId}"`,
    ` AIGC:ReservedCode1="${EMPTY_MD5}"`,
    ` AIGC:ContentPropagator="${producer}"`,
    ` AIGC:PropagateID="${produceId}"`,
    ` AIGC:ReservedCode2="${EMPTY_MD5}">`,
    `<AIGC:AIGC>${json}</AIGC:AIGC>`,
    '</rdf:Description>',
    '</rdf:RDF>',
    '</x:xmpmeta>',
    '<?xpacket end="w"?>',
  ].join('\n');
  return { xmp };
}

/** Decode a PNG blob into raw bytes. Returns null if the signature is invalid. */
async function decodePngBlob(blob: Blob): Promise<Uint8Array | null> {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  if (bytes.length < 8) return null;
  for (let i = 0; i < 8; i++) {
    if (bytes[i] !== PNG_SIGNATURE[i]) return null;
  }
  return bytes;
}

function insertChunkAfterIhdr(png: Uint8Array, chunk: Uint8Array): Uint8Array {
  if (png.length < 8 + 8) {
    // Not enough data to read the first chunk header; append safely.
    const out = new Uint8Array(png.length + chunk.length);
    out.set(png, 0);
    out.set(chunk, png.length);
    return out;
  }
  const ihdrLen = (png[8] << 24) | (png[9] << 16) | (png[10] << 8) | png[11];
  const ihdrEnd = 8 + 4 + 4 + ihdrLen + 4; // sig + len + type + data + crc
  const out = new Uint8Array(png.length + chunk.length);
  out.set(png.subarray(0, ihdrEnd), 0);
  out.set(chunk, ihdrEnd);
  out.set(png.subarray(ihdrEnd), ihdrEnd + chunk.length);
  return out;
}

function buildITextChunk(keyword: string, text: string): Uint8Array {
  const keywordBytes = AIGC_TEXT_ENCODER.encode(keyword);
  const textBytes = AIGC_TEXT_ENCODER.encode(text);
  // PNG spec iTXt data: keyword\0 + compFlag + compMethod + langTag\0 +
  // translatedKw\0 + text — i.e. five zero bytes after the keyword for the
  // uncompressed, empty-lang case. Detection platforms mis-parse that
  // canonical layout (their reader expects the text to begin with a NUL),
  // so emit one extra leading zero byte before the text. This matches the
  // byte layout that the platform accepts; verified by A/B upload.
  const chunkData = new Uint8Array(keywordBytes.length + 6 + textBytes.length);
  let offset = 0;
  chunkData.set(keywordBytes, offset);
  offset += keywordBytes.length;
  chunkData[offset++] = 0; // NUL separator after keyword
  chunkData[offset++] = 0; // compression flag: 0 = uncompressed
  chunkData[offset++] = 0; // compression method: 0
  chunkData[offset++] = 0; // language tag (empty) + NUL
  chunkData[offset++] = 0; // translated keyword (empty) + NUL
  chunkData[offset++] = 0; // extra leading NUL consumed by platform's iTXt reader
  chunkData.set(textBytes, offset);
  return buildPngChunk('iTXt', chunkData);
}

function buildAigcITextChunk(): Uint8Array {
  const { xmp } = buildAigcLabel();
  return buildITextChunk('XML:com.adobe.xmp', xmp);
}

export async function injectAigcPngMetadata(blob: Blob): Promise<Blob> {
  const png = await decodePngBlob(blob);
  if (!png) {
    return blob;
  }
  const out = insertChunkAfterIhdr(png, buildAigcITextChunk());
  return new Blob([out.buffer as ArrayBuffer], { type: 'image/png' });
}
