/**
 * Picks the sharpest JPEG frame that still fits Qwen-Omni-Realtime's image limit.
 *
 * DashScope does not resize images; it rejects them. input_image_buffer.append
 * accepts JPG up to 1080p, and the base64 image must stay under 256KB
 * (https://help.aliyun.com/zh/model-studio/client-events). A fixed 768px width
 * kept every frame small but made 4K screen text unreadable (issue #5606), so
 * frames now start at 1080p and only step down when the encoded JPEG is too big.
 */

// Source: Aliyun doc "建议编码前原始图片大小不超过190KB" (input_image_buffer.append,
// https://help.aliyun.com/zh/model-studio/client-events). 190KB raw is ~253KB in
// base64, below the 256KB hard limit that qwenOmniProtocol.ts enforces.
export const FRAME_MAX_JPEG_BYTES = 190 * 1024;

// Source: Aliyun doc "最高不超过 1080p" (same section): the short edge stays <= 1080.
const FRAME_MAX_SHORT_EDGE = 1080;

export interface FrameStep {
  maxLongEdge: number;
  quality: number;
}

// Long edges, best first. Sources: 1920 is the 1080p long edge from the Aliyun doc;
// 1280 is the measured fallback (4K dense table at 1280px q0.5 was 225KB base64 and
// qwen3.5-omni-flash-realtime read its title and 12-digit ids); 768 is the previous
// fixed width, kept as the last resort.
const FRAME_LONG_EDGES = [1920, 1280, 768];
// JPEG qualities, best first. Sources: 0.72 is the previous fixed quality; 0.5 is the
// measured readable quality above; 0.3 is the lowest measured quality (1920px q0.3 was
// still 361KB base64 on the 4K table, which is why resolution must also step down).
const FRAME_QUALITIES = [0.72, 0.5, 0.3];

/** Quality steps first, then resolution, so text keeps its pixels as long as possible. */
export const FRAME_STEPS: readonly FrameStep[] = FRAME_LONG_EDGES.flatMap((maxLongEdge) =>
  FRAME_QUALITIES.map((quality) => ({ maxLongEdge, quality })),
);

export interface FrameSize {
  width: number;
  height: number;
}

/** Scales down to the step's long edge and the 1080p short edge; never upscales. */
export function fitFrameSize(sourceWidth: number, sourceHeight: number, maxLongEdge: number): FrameSize {
  const longEdge = Math.max(sourceWidth, sourceHeight);
  const shortEdge = Math.min(sourceWidth, sourceHeight);
  const scale = Math.min(1, maxLongEdge / longEdge, FRAME_MAX_SHORT_EDGE / shortEdge);
  return {
    width: Math.max(1, Math.round(sourceWidth * scale)),
    height: Math.max(1, Math.round(sourceHeight * scale)),
  };
}

interface ResolvedStep extends FrameSize {
  quality: number;
}

/** Steps for one source size; steps that collapse to the same output are tried once. */
export function frameStepsFor(sourceWidth: number, sourceHeight: number): ResolvedStep[] {
  const seen = new Set<string>();
  const steps: ResolvedStep[] = [];
  for (const step of FRAME_STEPS) {
    const size = fitFrameSize(sourceWidth, sourceHeight, step.maxLongEdge);
    const key = `${size.width}x${size.height}@${step.quality}`;
    if (seen.has(key)) continue;
    seen.add(key);
    steps.push({ ...size, quality: step.quality });
  }
  return steps;
}

export interface EncodedFrame<T extends { size: number }> extends ResolvedStep {
  blob: T;
}

/** Renders the source at the given size and quality; null means the capture was cancelled. */
export type FrameRenderer<T extends { size: number }> = (
  width: number,
  height: number,
  quality: number,
) => Promise<T | null>;

/**
 * Remembers the last step that fitted for each source, so a busy 4K screen does
 * not re-encode the whole ladder every capture. Each capture first tries one
 * step better than the remembered one, so the frame climbs back to full
 * resolution once the picture gets simpler. Cost: at most one extra encode per
 * capture while a source sits below the top step.
 */
export class FrameEncoder {
  private readonly stepBySource = new Map<string, number>();

  constructor(private readonly maxBytes = FRAME_MAX_JPEG_BYTES) {}

  async encode<T extends { size: number }>(
    sourceId: string,
    sourceWidth: number,
    sourceHeight: number,
    render: FrameRenderer<T>,
  ): Promise<EncodedFrame<T> | null> {
    const steps = frameStepsFor(sourceWidth, sourceHeight);
    const remembered = Math.min(this.stepBySource.get(sourceId) ?? 0, steps.length - 1);
    for (let index = Math.max(0, remembered - 1); index < steps.length; index += 1) {
      const step = steps[index];
      const blob = await render(step.width, step.height, step.quality);
      if (!blob) return null;
      if (blob.size <= this.maxBytes) {
        this.stepBySource.set(sourceId, index);
        return { ...step, blob };
      }
    }
    // Even the smallest step is too big: drop the frame instead of breaking the socket.
    this.stepBySource.set(sourceId, steps.length - 1);
    return null;
  }

}

export interface CapturedVideoFrame extends ResolvedStep {
  dataUrl: string;
  jpegBytes: number;
}

function canvasToJpeg(canvas: HTMLCanvasElement, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', quality));
}

function blobToDataUrl(blob: Blob): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === 'string' ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(blob);
  });
}

/** Captures the current video frame as the sharpest JPEG data URL within the budget. */
export async function captureVideoFrame(
  encoder: FrameEncoder,
  video: HTMLVideoElement,
  canvas: HTMLCanvasElement,
  sourceId: string,
  isCancelled: () => boolean = () => false,
): Promise<CapturedVideoFrame | null> {
  if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return null;
  if (!video.videoWidth || !video.videoHeight) return null;
  const context = canvas.getContext('2d');
  if (!context) return null;

  // Draw once per output size; lower quality steps re-encode the same pixels.
  let drawnSize = '';
  const encoded = await encoder.encode(sourceId, video.videoWidth, video.videoHeight, async (width, height, quality) => {
    if (isCancelled()) return null;
    const size = `${width}x${height}`;
    if (drawnSize !== size) {
      if (canvas.width !== width) canvas.width = width;
      if (canvas.height !== height) canvas.height = height;
      context.drawImage(video, 0, 0, width, height);
      drawnSize = size;
    }
    const blob = await canvasToJpeg(canvas, quality);
    return isCancelled() ? null : blob;
  });
  if (!encoded || isCancelled()) return null;
  const dataUrl = await blobToDataUrl(encoded.blob);
  if (!dataUrl || isCancelled()) return null;
  return {
    width: encoded.width,
    height: encoded.height,
    quality: encoded.quality,
    jpegBytes: encoded.blob.size,
    dataUrl,
  };
}
