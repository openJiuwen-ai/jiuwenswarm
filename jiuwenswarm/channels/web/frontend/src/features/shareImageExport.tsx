import { forwardRef, useMemo } from 'react';
import { applyStyle } from 'html-to-image/es/apply-style';
import { cloneNode as cloneHtmlNode } from 'html-to-image/es/clone-node';
import { embedImages } from 'html-to-image/es/embed-images';
import { embedWebFonts } from 'html-to-image/es/embed-webfonts';
import type { Options as HtmlToImageOptions } from 'html-to-image/es/types';
import { useTranslation } from 'react-i18next';
import { ChatTimelineList } from '../components/ChatPanel/MessageList';
import { MarkdownMessageBody } from '../components/ChatPanel/MessageItem';
import { TeamMemberAvatar } from '../components/TeamMemberAvatar';
import { getMemberDisplayName } from '../components/teamArea/shared';
import { formatTeamEventTime, parseTeamEventMessage, type ParsedTeamEvent } from '../components/ChatPanel/teamEventUtils';
import { isUserMember } from '../utils/teamMemberAvatar';
import { parseHistoryJsonFileToTimelinePreview } from './historyRestore';
import { parseTeamHistoryPanelRecords } from './teamHistoryPanelRestore';
import { isA2UIClientEventContent } from './a2ui/a2uiContent';
import { getSvgNaturalHeight, getSvgNaturalWidth } from '../utils/svgDimensions';
import { generateUuidV4 } from '../utils/uuid';
import {
  ReusableShareImageClone,
  SHARE_IMAGE_PIXEL_RATIO,
  SHARE_IMAGE_WIDTH,
  cloneShareImageTreeInBlocks,
  getShareImageOutputDimensions,
  getShareImageTileSourceHeight,
  shouldIncludeShareImageCloneNode,
} from './shareImageRaster';
import { PNG_SIGNATURE, StreamingPngEncoder, buildPngChunk } from './streamingPng';
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

function yieldToBrowser(): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, 0));
}

async function prepareShareImageClone(node: HTMLElement, options: HtmlToImageOptions): Promise<ReusableShareImageClone> {
  const includeContentNode = (candidate: HTMLElement): boolean =>
    shouldIncludeShareImageCloneNode(candidate) && (!options.filter || options.filter(candidate));
  const clone = await cloneShareImageTreeInBlocks(
    node,
    async (source, excludedBlocks) => {
      const clonedNode = await cloneHtmlNode(
        source,
        {
          ...options,
          filter: candidate => !excludedBlocks.has(candidate) && includeContentNode(candidate),
        },
        true,
      );
      if (!(clonedNode instanceof HTMLElement)) {
        throw new Error('share_image_clone_failed');
      }
      return clonedNode;
    },
    yieldToBrowser,
  );

  await embedWebFonts(clone, options);
  await embedImages(clone, options);
  applyStyle(clone, options);
  clone.setAttribute('xmlns', 'http://www.w3.org/1999/xhtml');
  return new ReusableShareImageClone(node, clone);
}

function createShareImageMarkup(preparedClone: ReusableShareImageClone, sourceY: number, sourceHeight: number): string {
  const clone = preparedClone.prepareTile(sourceY, sourceHeight);
  return new XMLSerializer().serializeToString(clone);
}

async function loadShareImageSvg(svg: string): Promise<HTMLImageElement> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === 'string') {
        resolve(reader.result);
      } else {
        reject(new Error('share_image_svg_data_url_failed'));
      }
    };
    reader.onerror = () => reject(new Error('share_image_svg_data_url_failed'));
    reader.onabort = () => reject(new Error('share_image_svg_data_url_failed'));
    reader.readAsDataURL(new Blob([svg], { type: 'image/svg+xml;charset=utf-8' }));
  });
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('share_image_svg_decode_failed'));
    image.src = dataUrl;
  });
}

function createShareImageTileSvg(markup: string, width: number, sourceY: number, sourceHeight: number): string {
  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${sourceHeight}" viewBox="0 0 ${width} ${sourceHeight}">`,
    `<foreignObject x="0" y="0" width="100%" height="100%" externalResourcesRequired="true">`,
    `<div xmlns="http://www.w3.org/1999/xhtml" style="position:relative;width:${width}px;height:${sourceHeight}px;overflow:hidden">`,
    `<div style="width:${width}px;transform:translateY(${-sourceY}px);transform-origin:top left">`,
    markup,
    '</div></div></foreignObject></svg>',
  ].join('');
}

async function rasterizeShareImage(node: HTMLElement, options: HtmlToImageOptions, width: number, height: number, backgroundColor: string): Promise<Blob> {
  if (width !== SHARE_IMAGE_WIDTH) {
    throw new Error('share_image_invalid_width');
  }
  const [outputWidth, outputHeight] = getShareImageOutputDimensions(height);
  const tileSourceHeight = getShareImageTileSourceHeight();
  const encoder = new StreamingPngEncoder(outputWidth, outputHeight);
  let preparedClone: ReusableShareImageClone | null = null;

  try {
    preparedClone = await prepareShareImageClone(node, options);
    const canvas = document.createElement('canvas');
    canvas.width = outputWidth;
    canvas.height = tileSourceHeight * SHARE_IMAGE_PIXEL_RATIO;
    const context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) {
      throw new Error('share_image_canvas_context_unavailable');
    }

    for (let sourceY = 0; sourceY < height; sourceY += tileSourceHeight) {
      const sourceHeight = Math.min(tileSourceHeight, height - sourceY);
      const renderedHeight = sourceHeight * SHARE_IMAGE_PIXEL_RATIO;
      if (canvas.height !== renderedHeight) {
        canvas.height = renderedHeight;
      }
      context.fillStyle = backgroundColor;
      context.fillRect(0, 0, outputWidth, renderedHeight);

      const markup = createShareImageMarkup(preparedClone, sourceY, sourceHeight);
      const image = await loadShareImageSvg(createShareImageTileSvg(markup, width, sourceY, sourceHeight));
      context.drawImage(image, 0, 0, outputWidth, renderedHeight);
      const rgba = context.getImageData(0, 0, outputWidth, renderedHeight).data;
      await encoder.appendRgbaRows(rgba, renderedHeight);
      await nextFrame();
    }
    return encoder.finish([buildAigcITextChunk()]);
  } catch (error) {
    await encoder.abort(error);
    throw error;
  } finally {
    preparedClone?.restore();
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
  );
});

function nextFrame(): Promise<void> {
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

interface SvgSnapshot {
  svg: SVGSVGElement;
  width: string | null;
  height: string | null;
  styleWidth: string;
  styleHeight: string;
  styleMaxWidth: string;
}

/**
 * Scales down any Mermaid SVG that is wider than its container so the full
 * diagram fits inside the share image without being clipped horizontally.
 * Returns a cleanup function that restores the original attributes/styles.
 */
function fitMermaidDiagramsForExport(node: HTMLElement): () => void {
  const svgs = Array.from(node.querySelectorAll<SVGSVGElement>('.share-image-document .mermaid-canvas svg'));
  const snapshots: SvgSnapshot[] = [];

  for (const svg of svgs) {
    const naturalWidth = getSvgNaturalWidth(svg);
    const naturalHeight = getSvgNaturalHeight(svg);
    if (naturalWidth <= 0 || naturalHeight <= 0) continue;

    const container = svg.closest<HTMLElement>('.mermaid-canvas') ?? svg.parentElement;
    const containerWidth = container?.clientWidth ?? 0;
    if (containerWidth <= 0 || naturalWidth <= containerWidth) continue;

    const ratio = containerWidth / naturalWidth;
    snapshots.push({
      svg,
      width: svg.getAttribute('width'),
      height: svg.getAttribute('height'),
      styleWidth: svg.style.width,
      styleHeight: svg.style.height,
      styleMaxWidth: svg.style.maxWidth,
    });

    svg.setAttribute('width', String(containerWidth));
    svg.setAttribute('height', String(naturalHeight * ratio));
    svg.style.width = `${containerWidth}px`;
    svg.style.height = `${naturalHeight * ratio}px`;
    svg.style.maxWidth = 'none';
  }

  return () => {
    for (const snapshot of snapshots) {
      const { svg, width, height, styleWidth, styleHeight, styleMaxWidth } = snapshot;
      if (width === null) svg.removeAttribute('width');
      else svg.setAttribute('width', width);
      if (height === null) svg.removeAttribute('height');
      else svg.setAttribute('height', height);
      svg.style.width = styleWidth;
      svg.style.height = styleHeight;
      svg.style.maxWidth = styleMaxWidth;
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

export async function exportShareImageNode(node: HTMLElement): Promise<Blob> {
  await document.fonts?.ready;
  const restoreImages = await prepareImagesForExport(node);
  let restoreMermaidDiagrams = (): void => {};
  try {
    await waitForMermaidDiagrams(node);
    await nextFrame();

    // Scale down wide Mermaid diagrams so they are not clipped in the exported
    // image. The export DOM must remain mounted until the SVG markup is built.
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
    return rasterizeShareImage(node, options, SHARE_IMAGE_WIDTH, height, backgroundColor);
  } finally {
    restoreMermaidDiagrams();
    restoreImages();
  }
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
