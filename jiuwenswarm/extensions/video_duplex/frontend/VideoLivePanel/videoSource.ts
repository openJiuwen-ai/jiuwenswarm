export interface RealtimeVideoFrame {
  data_url: string;
  source_id: string;
}

/** Keeps the official ~1 fps aggregate cadence while rotating across sources. */
export class RealtimeVideoFrameScheduler {
  private lastSentAt = Number.NEGATIVE_INFINITY;
  private nextSourceIndex = 0;
  private readonly lastSentFrames = new Map<string, RealtimeVideoFrame>();

  constructor(private readonly minimumIntervalMs = 1_000) {}

  take<T extends RealtimeVideoFrame>(frames: readonly T[], now = Date.now()): T | null {
    if (now - this.lastSentAt < this.minimumIntervalMs) return null;

    const latestBySource = new Map<string, T>();
    frames.forEach((frame) => latestBySource.set(frame.source_id, frame));
    const latest = [...latestBySource.values()];
    if (latest.length === 0) return null;

    for (let offset = 0; offset < latest.length; offset += 1) {
      const index = (this.nextSourceIndex + offset) % latest.length;
      const frame = latest[index];
      if (this.lastSentFrames.get(frame.source_id) === frame) continue;
      this.lastSentAt = now;
      this.nextSourceIndex = (index + 1) % latest.length;
      this.lastSentFrames.set(frame.source_id, frame);
      return frame;
    }
    return null;
  }
}

export type RealtimeVideoSource = 'camera' | 'file' | 'screen' | null;

export interface RealtimeScreenSource {
  id: string;
}

interface VideoSourceReadiness {
  source: RealtimeVideoSource;
  cameraStream: MediaStream | null;
  screens: readonly RealtimeScreenSource[];
  screenStreams: ReadonlyMap<string, MediaStream>;
  video: HTMLMediaElement | null;
}

interface FirstFrameWaitOptions {
  timeoutMs?: number;
  pollMs?: number;
  now?: () => number;
  wait?: (delayMs: number) => Promise<void>;
}

function hasLiveVideoTrack(stream: MediaStream | null | undefined): boolean {
  return Boolean(stream?.getVideoTracks().some((track) => track.readyState === 'live'));
}

export function isVideoSourceReady({
  source,
  cameraStream,
  screens,
  screenStreams,
  video,
}: VideoSourceReadiness): boolean {
  if (source === 'camera') return hasLiveVideoTrack(cameraStream);
  if (source === 'screen') {
    return screens.length > 0
      && screens.every((screen) => hasLiveVideoTrack(screenStreams.get(screen.id)));
  }
  if (source === 'file') return Boolean(video && !video.paused);
  return true;
}

export async function waitForFirstVideoFrame(
  isSourceReady: () => boolean,
  hasFrame: () => boolean,
  {
    timeoutMs = 5_000,
    pollMs = 50,
    now = Date.now,
    wait = (delayMs) => new Promise((resolve) => window.setTimeout(resolve, delayMs)),
  }: FirstFrameWaitOptions = {},
): Promise<boolean> {
  const deadline = now() + timeoutMs;
  while (isSourceReady() && !hasFrame() && now() < deadline) await wait(pollMs);
  return isSourceReady() && hasFrame();
}
