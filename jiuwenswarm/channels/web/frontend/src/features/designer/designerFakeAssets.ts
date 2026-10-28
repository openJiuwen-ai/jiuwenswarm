/**
 * Procedural fake outputs for Designer mock runs.
 * Image/video are generated once via Canvas / MediaRecorder (no extra libraries).
 */

export const DESIGNER_FAKE_TEXT = '实例文本';

export const DESIGNER_FAKE_IMAGE_WIDTH = 720;
export const DESIGNER_FAKE_IMAGE_HEIGHT = 1280;
export const DESIGNER_FAKE_VIDEO_FPS = 12;
export const DESIGNER_FAKE_VIDEO_DURATION_SEC = 3;

type FakeAssetCache = {
  imageUrl: string | null;
  videoUrl: string | null;
  imagePromise: Promise<string> | null;
  videoPromise: Promise<string> | null;
};

const cache: FakeAssetCache = {
  imageUrl: null,
  videoUrl: null,
  imagePromise: null,
  videoPromise: null,
};

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function pickRecorderMimeType(): string {
  const candidates = [
    'video/webm;codecs=vp9',
    'video/webm;codecs=vp8',
    'video/webm',
  ];
  for (const type of candidates) {
    if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(type)) {
      return type;
    }
  }
  return 'video/webm';
}

function drawPortraitFrame(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  frameIndex: number,
  totalFrames: number,
): void {
  const t = totalFrames <= 1 ? 0 : frameIndex / (totalFrames - 1);
  const gradient = ctx.createLinearGradient(0, 0, width, height);
  gradient.addColorStop(0, `hsl(${210 + t * 40}, 62%, ${42 + t * 10}%)`);
  gradient.addColorStop(1, `hsl(${320 - t * 30}, 55%, ${28 + t * 8}%)`);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, width, height);

  ctx.fillStyle = 'rgba(255,255,255,0.14)';
  const barY = Math.floor((height - 120) * t);
  ctx.fillRect(48, barY, width - 96, 120);

  ctx.fillStyle = 'rgba(255,255,255,0.92)';
  ctx.font = '600 48px sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('FAKE', width / 2, height / 2 - 24);
  ctx.font = '400 28px sans-serif';
  ctx.fillText('720×1280', width / 2, height / 2 + 28);
  if (totalFrames > 1) {
    ctx.font = '400 22px sans-serif';
    ctx.fillText(`${frameIndex + 1}/${totalFrames}`, width / 2, height / 2 + 68);
  }
}

async function createFakeJpegBlob(): Promise<Blob> {
  const canvas = document.createElement('canvas');
  canvas.width = DESIGNER_FAKE_IMAGE_WIDTH;
  canvas.height = DESIGNER_FAKE_IMAGE_HEIGHT;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('2d canvas unavailable');
  }
  drawPortraitFrame(ctx, canvas.width, canvas.height, 0, 1);
  const blob = await new Promise<Blob | null>((resolve) => {
    canvas.toBlob((value) => resolve(value), 'image/jpeg', 0.86);
  });
  if (!blob) {
    throw new Error('failed to encode fake jpeg');
  }
  return blob;
}

async function createFakeVideoBlob(): Promise<Blob> {
  if (typeof MediaRecorder === 'undefined') {
    throw new Error('MediaRecorder unavailable');
  }

  const width = DESIGNER_FAKE_IMAGE_WIDTH;
  const height = DESIGNER_FAKE_IMAGE_HEIGHT;
  const fps = DESIGNER_FAKE_VIDEO_FPS;
  const durationSec = DESIGNER_FAKE_VIDEO_DURATION_SEC;
  const totalFrames = fps * durationSec;

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  if (!ctx) {
    throw new Error('2d canvas unavailable');
  }

  drawPortraitFrame(ctx, width, height, 0, totalFrames);
  const stream = canvas.captureStream(fps);
  const mimeType = pickRecorderMimeType();
  const chunks: BlobPart[] = [];
  const recorder = new MediaRecorder(stream, {
    mimeType,
    videoBitsPerSecond: 2_500_000,
  });

  const stopped = new Promise<Blob>((resolve, reject) => {
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) {
        chunks.push(event.data);
      }
    };
    recorder.onerror = () => {
      reject(new Error('MediaRecorder failed'));
    };
    recorder.onstop = () => {
      resolve(new Blob(chunks, { type: mimeType.split(';')[0] || 'video/webm' }));
    };
  });

  recorder.start(200);
  const frameDelayMs = 1000 / fps;
  for (let frame = 0; frame < totalFrames; frame += 1) {
    drawPortraitFrame(ctx, width, height, frame, totalFrames);
    await sleep(frameDelayMs);
  }
  if (recorder.state !== 'inactive') {
    recorder.stop();
  }
  for (const track of stream.getTracks()) {
    track.stop();
  }
  return stopped;
}

export async function ensureFakeImageUrl(): Promise<string> {
  if (cache.imageUrl) return cache.imageUrl;
  if (!cache.imagePromise) {
    cache.imagePromise = createFakeJpegBlob()
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        cache.imageUrl = url;
        return url;
      })
      .catch((error) => {
        cache.imagePromise = null;
        throw error;
      });
  }
  return cache.imagePromise;
}

export async function ensureFakeVideoUrl(): Promise<string> {
  if (cache.videoUrl) return cache.videoUrl;
  if (!cache.videoPromise) {
    cache.videoPromise = createFakeVideoBlob()
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        cache.videoUrl = url;
        return url;
      })
      .catch((error) => {
        cache.videoPromise = null;
        throw error;
      });
  }
  return cache.videoPromise;
}

/** Warm image + video blobs once; safe to call repeatedly. */
export async function ensureDesignerFakeAssets(): Promise<{
  imageUrl: string;
  videoUrl: string;
}> {
  const [imageUrl, videoUrl] = await Promise.all([
    ensureFakeImageUrl(),
    ensureFakeVideoUrl(),
  ]);
  return { imageUrl, videoUrl };
}

export function getCachedFakeImageUrl(): string | null {
  return cache.imageUrl;
}

export function getCachedFakeVideoUrl(): string | null {
  return cache.videoUrl;
}
