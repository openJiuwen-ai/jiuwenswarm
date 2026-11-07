import { createContext, useContext } from 'react';

export type MediaSurfaceRole = 'background' | 'splash';
export type MediaSurfaceEvent = 'ready' | 'heartbeat' | 'complete' | 'error';

/** Host-owned media behaviour; plugin props cannot change any of it. */
export interface MediaSurfaceHostPolicy {
  role: MediaSurfaceRole;
  /** Play silently whatever the plugin's muted prop says; HTML pages get no autoplay delegation either. */
  forceMuted: boolean;
  /** How long an HTML document may take to report ready before the poster replaces it. */
  readyTimeoutMs: number;
  /** Show only the poster under reduced motion when one is provided. */
  honorReducedMotion: boolean;
  /** Whether a video may follow its loop prop. */
  allowLoop: boolean;
  /** Reports mount and unmount so the host can mark the document while a surface is shown. */
  onPresence?: (present: boolean) => void;
  /** Playback events for hosts that time the surface, such as the splash gate. */
  onSurfaceEvent?: (event: MediaSurfaceEvent) => void;
}

export const BACKGROUND_SURFACE_POLICY: MediaSurfaceHostPolicy = {
  role: 'background',
  forceMuted: true,
  readyTimeoutMs: 15_000,
  honorReducedMotion: true,
  allowLoop: true,
};

/** The splash gate owns timing; the provider already received the reduced-motion setting. */
export const SPLASH_SURFACE_POLICY: MediaSurfaceHostPolicy = {
  role: 'splash',
  forceMuted: false,
  readyTimeoutMs: 60_000,
  honorReducedMotion: false,
  allowLoop: false,
};

// Outside the splash gate a MediaSurface can only be the application background.
export const MediaSurfacePolicyContext = createContext<MediaSurfaceHostPolicy>(BACKGROUND_SURFACE_POLICY);

export function useMediaSurfacePolicy(): MediaSurfaceHostPolicy {
  return useContext(MediaSurfacePolicyContext);
}
