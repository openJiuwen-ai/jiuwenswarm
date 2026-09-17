import type { FileDownloadItem, Message } from '../../types';
import { extractTokenFromDownloadUrl, getFileIdentityKey, resolveFilePath } from '../../utils/fileDownloadDedup';

export interface ArtifactItem {
  id: string;
  name: string;
  size?: number;
  mimeType?: string;
  downloadUrl?: string;
  downloadToken?: string;
  path?: string;
  source: 'message';
  sourceMember?: string;
  timestamp?: number;
}

export function fileArtifactId(file: FileDownloadItem): string {
  return `file:${getFileIdentityKey(file)}`;
}

function normalizeDownloadUrl(downloadUrl?: string, downloadToken?: string): string | undefined {
  const normalizedUrl = downloadUrl?.trim();
  if (normalizedUrl) return normalizedUrl;
  const normalizedToken = downloadToken?.trim();
  if (normalizedToken) return `/file-api/download?token=${encodeURIComponent(normalizedToken)}`;
  return undefined;
}

function preferArtifact(existing: ArtifactItem, candidate: ArtifactItem): ArtifactItem {
  const existingTs = existing.timestamp || 0;
  const candidateTs = candidate.timestamp || 0;
  const existingHasUrl = Boolean(existing.downloadUrl || existing.downloadToken);
  const candidateHasUrl = Boolean(candidate.downloadUrl || candidate.downloadToken);

  let primary: ArtifactItem;
  let secondary: ArtifactItem;
  if (candidateTs > existingTs) {
    primary = candidate;
    secondary = existing;
  } else if (candidateTs < existingTs) {
    primary = existing;
    secondary = candidate;
  } else if (candidateHasUrl && !existingHasUrl) {
    primary = candidate;
    secondary = existing;
  } else {
    primary = existing;
    secondary = candidate;
  }

  if (!primary.downloadUrl && secondary.downloadUrl) {
    primary = {
      ...primary,
      downloadUrl: secondary.downloadUrl,
      downloadToken: primary.downloadToken || secondary.downloadToken,
      size: primary.size ?? secondary.size,
      mimeType: primary.mimeType || secondary.mimeType,
    };
  }
  if (!primary.path && secondary.path) {
    primary = { ...primary, path: secondary.path };
  }
  return primary;
}

function messageTime(message: Message): number {
  const parsed = Date.parse(message.timestamp);
  return Number.isFinite(parsed) ? parsed : 0;
}

function hasArtifactResource(file: FileDownloadItem): boolean {
  return Boolean(file.download_url?.trim() || file.download_token?.trim() || file.path?.trim());
}

function fileItemToArtifact(file: FileDownloadItem, message: Message): ArtifactItem {
  const name = file.name.trim();
  const downloadToken = file.download_token?.trim() || extractTokenFromDownloadUrl(file.download_url);
  return {
    id: fileArtifactId(file),
    name,
    size: file.size,
    mimeType: file.mime_type,
    downloadUrl: normalizeDownloadUrl(file.download_url, downloadToken),
    downloadToken,
    path: resolveFilePath({
      path: file.path,
      download_token: downloadToken,
      download_url: file.download_url,
    }),
    source: 'message',
    timestamp: messageTime(message),
  };
}

export function buildArtifacts(messages: Message[]): ArtifactItem[] {
  const artifacts: ArtifactItem[] = [];

  messages.forEach(message => {
    message.fileItems?.forEach(file => {
      if (!file.name?.trim() || !hasArtifactResource(file)) return;
      artifacts.push(fileItemToArtifact(file, message));
    });
  });

  const deduped = new Map<string, ArtifactItem>();
  artifacts.forEach(artifact => {
    const key = getFileIdentityKey({
      name: artifact.name,
      size: artifact.size,
      path: artifact.path,
      download_url: artifact.downloadUrl,
      download_token: artifact.downloadToken,
    });
    const existing = deduped.get(key);
    deduped.set(key, existing ? preferArtifact(existing, artifact) : artifact);
  });

  return Array.from(deduped.values()).sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
}
