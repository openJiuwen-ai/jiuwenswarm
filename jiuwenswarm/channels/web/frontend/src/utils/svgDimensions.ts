function parseSvgViewBox(svg: SVGSVGElement): { width: number; height: number } | null {
  const viewBox = svg.getAttribute('viewBox');
  if (!viewBox) return null;

  const parts = viewBox
    .split(/[,\s]+/)
    .map(Number)
    .filter(Number.isFinite);

  if (parts.length >= 4 && parts[2] > 0 && parts[3] > 0) {
    return { width: parts[2], height: parts[3] };
  }
  return null;
}

function parsePixelAttribute(value: string | null): number | null {
  if (!value) return null;

  const trimmed = value.trim();
  // Reject percentages and non-pixel units. Allow bare numbers or explicit px.
  if (trimmed.endsWith('%')) return null;
  if (/[a-zA-Z]/.test(trimmed) && !trimmed.toLowerCase().endsWith('px')) return null;

  const n = parseFloat(trimmed);
  return Number.isFinite(n) && n > 0 ? n : null;
}

export function getSvgNaturalWidth(svg: SVGSVGElement): number {
  // Prefer viewBox because it describes the coordinate system of the actual
  // diagram content, regardless of any CSS or width attribute scaling.
  const viewBox = parseSvgViewBox(svg);
  if (viewBox) return viewBox.width;

  const widthAttr = parsePixelAttribute(svg.getAttribute('width'));
  if (widthAttr !== null) return widthAttr;

  try {
    const bbox = svg.getBBox();
    if (Number.isFinite(bbox.width) && bbox.width > 0) return bbox.width;
  } catch {
    // getBBox may fail if SVG is not visible
  }

  return 0;
}

export function getSvgNaturalHeight(svg: SVGSVGElement): number {
  const viewBox = parseSvgViewBox(svg);
  if (viewBox) return viewBox.height;

  const heightAttr = parsePixelAttribute(svg.getAttribute('height'));
  if (heightAttr !== null) return heightAttr;

  try {
    const bbox = svg.getBBox();
    if (Number.isFinite(bbox.height) && bbox.height > 0) return bbox.height;
  } catch {
    // getBBox may fail if SVG is not visible
  }

  return 0;
}
