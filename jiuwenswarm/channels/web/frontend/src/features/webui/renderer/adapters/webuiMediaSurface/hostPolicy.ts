import { createContext, useContext } from 'react';

export type MediaSurfaceRole = 'background';

/** Host-owned media behaviour; plugin props cannot change any of it. */
export interface MediaSurfaceHostPolicy {
  role: MediaSurfaceRole;
  /** Play silently whatever the plugin's muted prop says. */
  forceMuted: boolean;
  /** How long an HTML document may take to report ready before the poster replaces it. */
  readyTimeoutMs: number;
  /** Reports mount and unmount so the host can mark the document while a surface is shown. */
  onPresence?: (present: boolean) => void;
}

export const BACKGROUND_SURFACE_POLICY: MediaSurfaceHostPolicy = {
  role: 'background',
  forceMuted: true,
  readyTimeoutMs: 15_000,
};

// MediaSurface is only accepted by application.background, so its policy is the default.
export const MediaSurfacePolicyContext = createContext<MediaSurfaceHostPolicy>(BACKGROUND_SURFACE_POLICY);

export function useMediaSurfacePolicy(): MediaSurfaceHostPolicy {
  return useContext(MediaSurfacePolicyContext);
}
