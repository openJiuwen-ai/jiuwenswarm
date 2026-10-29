import { getSvgNaturalHeight, getSvgNaturalWidth } from '../../../utils/svgDimensions';
import { executeDesktopSave, type DesktopSaveOutcome } from '../../../utils/desktopSave';

const SVG_EXPORT_MAX_DIMENSION = 8192;
const SVG_EXPORT_MAX_AREA = 32_000_000;

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === 'string') resolve(reader.result);
      else reject(new Error('Blob data URL conversion returned no result'));
    };
    reader.onerror = () => reject(reader.error ?? new Error('Blob data URL conversion failed'));
    reader.onabort = () => reject(new Error('Blob data URL conversion was aborted'));
    reader.readAsDataURL(blob);
  });
}

export async function saveBlob(blob: Blob, filename: string): Promise<DesktopSaveOutcome> {
  const saveDataUrl = window.pywebview?.api?.save_data_url;
  if (!saveDataUrl) {
    downloadBlob(blob, filename);
    return 'saved';
  }

  const dataUrl = await blobToDataUrl(blob);
  return executeDesktopSave(() => saveDataUrl(dataUrl, filename));
}

async function loadSvgImage(svg: string): Promise<HTMLImageElement> {
  const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  try {
    const image = new Image();
    image.decoding = 'async';
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error('SVG image decode failed'));
      image.src = url;
    });
    return image;
  } finally {
    URL.revokeObjectURL(url);
  }
}

export async function convertSvgToPng(svg: string): Promise<Blob> {
  const image = await loadSvgImage(svg);
  const parsed = new DOMParser().parseFromString(svg, 'image/svg+xml');
  const root = parsed.documentElement as unknown as SVGSVGElement;
  const width = Math.ceil(getSvgNaturalWidth(root) || image.naturalWidth);
  const height = Math.ceil(getSvgNaturalHeight(root) || image.naturalHeight);
  const exceedsDimensionLimit = width > SVG_EXPORT_MAX_DIMENSION || height > SVG_EXPORT_MAX_DIMENSION;
  const exceedsAreaLimit = width * height > SVG_EXPORT_MAX_AREA;
  if (width <= 0 || height <= 0 || exceedsDimensionLimit || exceedsAreaLimit) {
    throw new Error('SVG export dimensions are unsupported');
  }

  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d');
  if (!context) {
    throw new Error('Canvas 2D context is unavailable');
  }
  context.drawImage(image, 0, 0, width, height);

  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(blob => {
      if (blob) resolve(blob);
      else reject(new Error('PNG encoding failed'));
    }, 'image/png');
  });
}
