import JSZip from 'jszip';

export interface ShareImageExportArtifact {
  blob: Blob;
  filename: string;
}

function normalizedPngFilename(filename: string): string {
  const safeFilename = filename.split(/[\\/]/).pop()?.trim() || 'jiuwenswarm-share.png';
  return /\.png$/i.test(safeFilename) ? safeFilename : `${safeFilename}.png`;
}

function shareImageFilenameStem(filename: string): string {
  return normalizedPngFilename(filename).replace(/\.png$/i, '');
}

export function getShareImagePartFilename(filename: string, index: number, count: number): string {
  if (!Number.isInteger(index) || !Number.isInteger(count) || index < 0 || count < 2 || index >= count) {
    throw new Error('share_image_part_index_invalid');
  }
  const digits = Math.max(2, String(count).length);
  const part = String(index + 1).padStart(digits, '0');
  const total = String(count).padStart(digits, '0');
  return `${shareImageFilenameStem(filename)}-part-${part}-of-${total}.png`;
}

export async function buildShareImageArtifact(
  pngParts: readonly (Blob | Uint8Array)[],
  filename: string,
  onProgress?: () => void,
): Promise<ShareImageExportArtifact> {
  if (pngParts.length === 0) {
    throw new Error('share_image_parts_empty');
  }
  if (pngParts.length === 1) {
    const part = pngParts[0];
    const blob = part instanceof Blob
      ? part
      : new Blob([new Uint8Array(part).buffer], { type: 'image/png' });
    return {
      blob,
      filename: normalizedPngFilename(filename),
    };
  }

  const archive = new JSZip();
  for (let index = 0; index < pngParts.length; index++) {
    archive.file(
      getShareImagePartFilename(filename, index, pngParts.length),
      pngParts[index],
      { binary: true, compression: 'STORE' },
    );
  }
  const bytes = await archive.generateAsync(
    { type: 'uint8array', compression: 'STORE', streamFiles: true },
    onProgress,
  );
  return {
    blob: new Blob([bytes.buffer as ArrayBuffer], { type: 'application/zip' }),
    filename: `${shareImageFilenameStem(filename)}.zip`,
  };
}
