import { getSvgNaturalHeight, getSvgNaturalWidth } from '../../../utils/svgDimensions';

export { downloadBlob, saveBlob } from '../../../utils/desktopSave';

const SVG_EXPORT_MAX_DIMENSION = 8192;
const SVG_EXPORT_MAX_AREA = 32_000_000;

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
