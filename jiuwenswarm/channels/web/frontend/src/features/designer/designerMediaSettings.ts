/** Canvas-wide generation settings shown in the node toolbar. */

import type {
  DesignerExecutionGraph,
  DesignerGraphNode,
  DesignerImageSettings,
  DesignerMediaCapabilities,
  DesignerVideoSettings,
} from './executionGraphTypes';
import { isUploadedMediaNode } from './mediaNodeConfig';

function storedField(metadata: Record<string, unknown> | undefined, key: string, field: string): string {
  const raw = metadata?.[key];
  if (!raw || typeof raw !== 'object') return '';
  const value = (raw as Record<string, unknown>)[field];
  return typeof value === 'string' ? value.trim() : '';
}

function allowedOr(value: string, allowed: string[], fallback: string): string {
  const wanted = value.toLowerCase();
  return allowed.find((item) => item.toLowerCase() === wanted) ?? fallback;
}

/** Stored canvas settings; values the active model does not list fall back to the server's snapped value. */
export function effectiveVideoSettings(
  metadata: Record<string, unknown> | undefined,
  caps: DesignerMediaCapabilities,
): DesignerVideoSettings {
  const fallback = caps.settings?.video;
  return {
    aspect_ratio: allowedOr(
      storedField(metadata, 'video_settings', 'aspect_ratio'),
      caps.video.ratios,
      fallback?.aspect_ratio ?? caps.video.default_ratio,
    ),
    resolution: allowedOr(
      storedField(metadata, 'video_settings', 'resolution'),
      caps.video.resolutions,
      fallback?.resolution ?? caps.video.default_resolution,
    ),
  };
}

export function effectiveImageSettings(
  metadata: Record<string, unknown> | undefined,
  caps: DesignerMediaCapabilities,
): DesignerImageSettings {
  const fallback = caps.settings?.image;
  return {
    aspect_ratio: allowedOr(
      storedField(metadata, 'image_settings', 'aspect_ratio'),
      caps.image.ratios,
      fallback?.aspect_ratio ?? caps.image.default_ratio,
    ),
    resolution: allowedOr(
      storedField(metadata, 'image_settings', 'resolution'),
      caps.image.resolutions,
      fallback?.resolution ?? caps.image.default_resolution,
    ),
  };
}

/** Stored music-node length, or null when the node leaves the film's own duration in place. */
export function effectiveAudioDurationSec(...candidates: unknown[]): number | null {
  for (const value of candidates) {
    const seconds = positiveWholeSeconds(value);
    if (seconds !== null) return seconds;
  }
  return null;
}

export function audioDurationParts(totalSeconds: number): { minutes: number; seconds: number } {
  const total = Math.max(0, Math.floor(totalSeconds));
  return { minutes: Math.floor(total / 60), seconds: total % 60 };
}

/** Button preview: whole minutes drop the seconds, under a minute drops the minutes. */
export function audioDurationLabelForm(totalSeconds: number): 'minutes' | 'seconds' | 'both' {
  const { minutes, seconds } = audioDurationParts(totalSeconds);
  if (minutes > 0 && seconds === 0) return 'minutes';
  if (minutes === 0) return 'seconds';
  return 'both';
}

/**
 * Minutes and seconds fields as one length. An empty field counts as zero; seconds may be 60 or more.
 * A zero total and non-integers are rejected; there is no maximum.
 */
export function parseAudioDurationFields(minutesRaw: string, secondsRaw: string): number | null {
  const minutes = minutesRaw.trim() ? parseNonNegativeInt(minutesRaw) : 0;
  const seconds = secondsRaw.trim() ? parseNonNegativeInt(secondsRaw) : 0;
  if (minutes === null || seconds === null) return null;
  const total = minutes * 60 + seconds;
  if (!Number.isSafeInteger(total) || total <= 0) return null;
  return total;
}

/** Music-node config with this length; null drops both length keys so the film's own duration applies. */
export function withAudioDuration(config: Record<string, unknown>, seconds: number | null): Record<string, unknown> {
  const next = { ...config };
  if (seconds === null) {
    delete next.duration_sec;
    delete next.film_duration_sec;
    return next;
  }
  next.duration_sec = seconds;
  next.film_duration_sec = seconds;
  return next;
}

function positiveWholeSeconds(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  return Math.floor(value);
}

function parseNonNegativeInt(raw: string): number | null {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return null;
  const value = Number(text);
  if (!Number.isSafeInteger(value)) return null;
  return value;
}

/** Whole seconds a video node generates at: its manual value, else the server's reading, clamped to the model. */
export function effectiveVideoDuration(nodeId: string, durationSec: unknown, caps: DesignerMediaCapabilities): number {
  const { min_sec: min, max_sec: max } = caps.video;
  const manual = typeof durationSec === 'number' && Number.isFinite(durationSec) ? Math.round(durationSec) : null;
  const seconds = manual ?? caps.video_durations?.[nodeId] ?? caps.video.default_sec;
  return Math.min(max, Math.max(min, seconds));
}

function videoPipeline(node: DesignerGraphNode): 'clip' | 'compose' | '' {
  const config = (node.config ?? {}) as { pipeline?: unknown; role?: unknown };
  for (const value of [config.pipeline, config.role]) {
    if (value === 'clip' || value === 'compose') return value;
  }
  if (node.id === 'n_clip' || node.id.startsWith('n_clip_')) return 'clip';
  if (node.id === 'n_compose' || node.id === 'n_final') return 'compose';
  return '';
}

/** Mirrors the server: a generated (not uploaded) video node that does not concat its video inputs. */
export function generatesTimedVideo(graph: DesignerExecutionGraph | null | undefined, nodeId: string): boolean {
  const node = graph?.nodes.find((item) => item.id === nodeId);
  if (!graph || !node || node.type !== 'video' || isUploadedMediaNode(node)) return false;
  const pipeline = videoPipeline(node);
  if (pipeline === 'clip') return true;
  if (pipeline === 'compose') return false;
  const declared = ((node.config ?? {}) as { inputs?: unknown }).inputs;
  const sources = new Set([
    ...(Array.isArray(declared) ? declared.map(String) : []),
    ...graph.edges.filter((edge) => edge.target === nodeId).map((edge) => edge.source),
  ]);
  sources.delete(nodeId);
  return !graph.nodes.some((item) => sources.has(item.id) && (item.type === 'video' || videoPipeline(item) !== ''));
}

const BACKEND_LABELS: Record<string, string> = {
  minimax: 'MiniMax',
  modelark: 'ModelArk',
  dashscope: 'DashScope',
  'vllm-omni': 'vLLM-Omni',
  openrouter: 'OpenRouter',
};

/** Configured model name; a provider that allows an empty model name (vLLM-Omni) shows its own name. */
export function generationModelLabel(slot: { configured_model: string; backend: string | null }): string {
  return slot.configured_model.trim() || (slot.backend ? (BACKEND_LABELS[slot.backend] ?? slot.backend) : '');
}
