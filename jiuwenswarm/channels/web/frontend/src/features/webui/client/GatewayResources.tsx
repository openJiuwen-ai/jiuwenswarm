import { useEffect, useState, useSyncExternalStore, type ImgHTMLAttributes, type VideoHTMLAttributes, type IframeHTMLAttributes } from 'react';
import { gatewayFetch } from '../../../services/gatewayFetch';
import { downloadBlob } from '../../../utils/desktopSave';
import { browserClientCapabilities as host } from './browserClient.ts';

export function needsGatewayResource(url: string): boolean {
  return host.getPackageAssetState(url) !== null
    || ((host.hasHTTPBindings() || host.isUnavailable()) && host.isGateway(url));
}

/** Retain each Blob only for its package activation or request context and component mount. */
export function useGatewayResourceState(url?: string): { url?: string; pending: boolean } {
  const getRevision = () => {
    const asset = url ? host.getPackageAssetState(url) : null;
    if (asset) return asset.status === 'active' ? `package:${asset.version}` : 'package:inactive';
    return `request:${host.getEpoch()}:${host.isChanging()}`;
  };
  const revision = useSyncExternalStore(
    (listener) => host.subscribe(() => listener()),
    getRevision,
    getRevision,
  );
  const bound = Boolean(url && needsGatewayResource(url));
  const [resource, setResource] = useState<{ url: string; revision: string; objectUrl?: string } | null>(null);
  useEffect(() => {
    const asset = url ? host.getPackageAssetState(url) : null;
    if (!url || !bound || asset?.status === 'inactive' || (!asset && host.isChanging())) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    let active = true;
    void gatewayFetch(url, { signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error('RESOURCE_UNAVAILABLE');
      const blob = await response.blob();
      if (!active) return;
      objectUrl = URL.createObjectURL(blob);
      setResource({ url, revision, objectUrl });
    }).catch(() => { if (active) setResource({ url, revision }); });
    return () => { active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [url, bound, revision]);
  if (!bound) return { url, pending: false };
  const current = resource && resource.url === url && resource.revision === revision ? resource : null;
  return { url: current?.objectUrl, pending: current === null };
}

export function useGatewayResource(url?: string): string | undefined {
  return useGatewayResourceState(url).url;
}

export function GatewayImage(props: ImgHTMLAttributes<HTMLImageElement>) {
  const resource = useGatewayResourceState(props.src);
  return resource.pending ? null : <img data-testid="webui-resource-image" {...props} src={resource.url} />;
}
export function GatewayVideo(props: VideoHTMLAttributes<HTMLVideoElement>) {
  const src = useGatewayResource(props.src);
  return <video data-testid="webui-resource-video" {...props} src={src} />;
}
export function GatewayFrame(props: IframeHTMLAttributes<HTMLIFrameElement>) {
  const src = useGatewayResource(props.src);
  return <iframe data-testid="webui-resource-frame" {...props} src={src} />;
}

export async function downloadGatewayResource(url: string, filename: string): Promise<void> {
  const response = await gatewayFetch(url);
  if (!response.ok) throw new Error('RESOURCE_UNAVAILABLE');
  downloadBlob(await response.blob(), filename);
}
