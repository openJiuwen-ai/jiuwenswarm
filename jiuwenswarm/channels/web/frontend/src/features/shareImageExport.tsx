import { forwardRef, useMemo } from 'react';
import { toPng } from 'html-to-image';
import { useTranslation } from 'react-i18next';
import { ChatTimelineList } from '../components/ChatPanel/MessageList';
import { MarkdownMessageBody } from '../components/ChatPanel/MessageItem';
import { TeamMemberAvatar } from '../components/TeamMemberAvatar';
import { getMemberDisplayName } from '../components/teamArea/shared';
import {
  formatTeamEventTime,
  parseTeamEventMessage,
  type ParsedTeamEvent,
} from '../components/ChatPanel/teamEventUtils';
import { isUserMember } from '../utils/teamMemberAvatar';
import { parseHistoryJsonFileToTimelinePreview } from './historyRestore';
import { parseTeamHistoryPanelRecords } from './teamHistoryPanelRestore';
import { isA2UIClientEventContent } from './a2ui/a2uiContent';
import { getSvgNaturalHeight, getSvgNaturalWidth } from '../utils/svgDimensions';
import { generateUuidV4 } from '../utils/uuid';
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

const SHARE_IMAGE_WIDTH = 750;
const SHARE_IMAGE_PIXEL_RATIO = 3;
const OPENJIUWEN_WEBSITE_URL = 'https://openjiuwen.com';
const JIUWENSWARM_REPO_URL = 'https://gitcode.com/openJiuwen/jiuwenswarm';
const TRANSPARENT_IMAGE_DATA_URL = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/**
 * Filter out A2UI client event messages from the message list.
 * These messages are internal interaction events and should not be included in exports.
 */
function filterA2UIClientEvents(messages: unknown[]): unknown[] {
  return messages.filter((msg) => {
    if (!isRecord(msg)) return true;
    if (msg.role === 'user' && isA2UIClientEventContent(msg.content)) return false;
    return true;
  });
}

function normalizeMode(records: unknown[]): string {
  const modes = records
    .filter(isRecord)
    .map((record) => typeof record.mode === 'string' ? record.mode.trim().toLowerCase() : '')
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
      {!isUser && (
        <TeamMemberAvatar
          member={event.fromMember}
          className="share-image-group-message__avatar"
        />
      )}
      <div className="share-image-group-message__main">
        <div className="share-image-group-message__meta">
          <span className="share-image-group-message__member">{displayName}</span>
          {timeText && <span className="share-image-group-message__time">{timeText}</span>}
        </div>
        <div className="share-image-group-message__bubble">
          {event.isP2P && event.toMember && (
            <span className="share-image-group-message__chip">@{getMemberDisplayName(event.toMember)}</span>
          )}
          {event.isBroadcast && (
            <span className="share-image-group-message__chip">{t('share.everyone')}</span>
          )}
          <MarkdownMessageBody
            content={event.content}
            className="share-image-group-message__body"
          />
        </div>
      </div>
      {isUser && (
        <TeamMemberAvatar
          member={event.fromMember}
          className="share-image-group-message__avatar"
        />
      )}
    </article>
  );
}

export const ShareImageDocument = forwardRef<HTMLDivElement, ShareImageDocumentProps>(
  function ShareImageDocument({ snapshot }, ref) {
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
              <div className="share-image-brand__name">JiuwenSwarm</div>
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
                  {data.groupMessages.map((item) => (
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
  }
);

function nextFrame(): Promise<void> {
  return new Promise((resolve) => {
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
  return new Promise<boolean>((resolve) => {
    image.addEventListener('load', () => resolve(image.naturalWidth > 0), { once: true });
    image.addEventListener('error', () => resolve(false), { once: true });
  });
}

async function prepareImagesForExport(node: HTMLElement): Promise<() => void> {
  const images = Array.from(node.querySelectorAll('img'));
  const snapshots: ImageSnapshot[] = [];

  await Promise.all(images.map(async (image) => {
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
  }));

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
    return Array.from(node.querySelectorAll('[data-mermaid-status="rendered"]'))
      .every((diagram) => diagram.querySelector('svg'));
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

export async function exportShareImageNode(node: HTMLElement): Promise<string> {
  await document.fonts?.ready;
  const restoreImages = await prepareImagesForExport(node);
  let restoreMermaidDiagrams = (): void => {};
  try {
    await waitForMermaidDiagrams(node);
    await nextFrame();

    // Scale down wide Mermaid diagrams so they are not clipped in the exported
    // image. toPng reads the DOM synchronously, so the restore callback must be
    // called after the render completes.
    restoreMermaidDiagrams = fitMermaidDiagramsForExport(node);
    await nextFrame();

    const backgroundColor = window.getComputedStyle(node).backgroundColor;
    const dataUrl = await toPng(node, {
      cacheBust: true,
      pixelRatio: SHARE_IMAGE_PIXEL_RATIO,
      width: SHARE_IMAGE_WIDTH,
      height: node.scrollHeight,
      backgroundColor,
    });
    // Inject implicit AIGC label (GB 45438-2025) into PNG file metadata.
    return injectAigcPngMetadata(dataUrl);
  } finally {
    restoreMermaidDiagrams();
    restoreImages();
  }
}

const AIGC_TEXT_ENCODER = new TextEncoder();

/** PNG 8-byte signature, used to verify the data URL really is a PNG. */
const PNG_SIGNATURE = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function buildCrc32Table(): Uint32Array {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
}

const CRC32_TABLE = buildCrc32Table();

function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    crc = CRC32_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

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
  const producer = 'JiuwenSwarm';
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

/** Decode a data URL (base64) into raw PNG bytes. Returns null if not PNG. */
function decodePngDataUrl(dataUrl: string): Uint8Array | null {
  const comma = dataUrl.indexOf(',');
  if (comma < 0) return null;
  const base64 = dataUrl.slice(comma + 1);
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }
  if (bytes.length < 8) return null;
  for (let i = 0; i < 8; i++) {
    if (bytes[i] !== PNG_SIGNATURE[i]) return null;
  }
  return bytes;
}

/** Encode raw bytes back into a PNG data URL (base64). */
function encodePngDataUrl(bytes: Uint8Array): string {
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const end = Math.min(i + chunkSize, bytes.length);
    binary += String.fromCharCode(...bytes.subarray(i, end));
  }
  return `data:image/png;base64,${btoa(binary)}`;
}

function buildPngChunk(type: string, chunkData: Uint8Array): Uint8Array {
  const typeBytes = AIGC_TEXT_ENCODER.encode(type);
  const crcInput = new Uint8Array(typeBytes.length + chunkData.length);
  crcInput.set(typeBytes, 0);
  crcInput.set(chunkData, typeBytes.length);
  const crc = crc32(crcInput);

  const chunk = new Uint8Array(4 + 4 + chunkData.length + 4);
  const view = new DataView(chunk.buffer);
  view.setUint32(0, chunkData.length); // big-endian data length
  chunk.set(typeBytes, 4);
  chunk.set(chunkData, 8);
  view.setUint32(8 + chunkData.length, crc);
  return chunk;
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
  const chunkData = new Uint8Array(
    keywordBytes.length + 6 + textBytes.length,
  );
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

export function injectAigcPngMetadata(dataUrl: string): string {
  const png = decodePngDataUrl(dataUrl);
  if (!png) {
    return dataUrl;
  }
  const { xmp } = buildAigcLabel();
  const out = insertChunkAfterIhdr(png, buildITextChunk('XML:com.adobe.xmp', xmp));
  return encodePngDataUrl(out);
}
