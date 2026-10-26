export type AxisSpan = {
  start: number;
  size: number;
};

export type CategoryBand = {
  start: number;
  size: number;
};

export type AxisDomain = {
  minimum: number;
  maximum: number;
};

export function ensureNonZeroAxisSpan(minimum: number, maximum: number, step: number): AxisDomain {
  if (![minimum, maximum, step].every(Number.isFinite)) throw new TypeError('Chart axis values must be finite');
  if (maximum < minimum) throw new RangeError('Chart axis maximum must not be less than its minimum');
  if (step <= 0) throw new RangeError('Chart axis step must be positive');
  return {
    minimum,
    maximum: maximum === minimum ? maximum + step : maximum,
  };
}

export function linearPosition(value: number, domainStart: number, domainEnd: number, rangeStart: number, rangeEnd: number): number {
  if (![value, domainStart, domainEnd, rangeStart, rangeEnd].every(Number.isFinite)) throw new TypeError('Chart coordinates must be finite');
  if (domainStart === domainEnd) throw new RangeError('Chart domain must have a non-zero span');
  return rangeStart + ((value - domainStart) / (domainEnd - domainStart)) * (rangeEnd - rangeStart);
}

export function spanFromBaseline(valuePosition: number, baselinePosition: number): AxisSpan {
  if (!Number.isFinite(valuePosition) || !Number.isFinite(baselinePosition)) throw new TypeError('Chart coordinates must be finite');
  return {
    start: Math.min(valuePosition, baselinePosition),
    size: Math.abs(valuePosition - baselinePosition),
  };
}

export function categoryCenter(index: number, categoryCount: number, rangeStart: number, rangeEnd: number): number {
  validateCategoryIndex(index, categoryCount);
  if (!Number.isFinite(rangeStart) || !Number.isFinite(rangeEnd)) throw new TypeError('Chart coordinates must be finite');
  return rangeStart + ((index + 0.5) / categoryCount) * (rangeEnd - rangeStart);
}

export function categoryPoint(index: number, categoryCount: number, rangeStart: number, rangeEnd: number): number {
  validateCategoryIndex(index, categoryCount);
  if (!Number.isFinite(rangeStart) || !Number.isFinite(rangeEnd)) throw new TypeError('Chart coordinates must be finite');
  if (categoryCount === 1) return (rangeStart + rangeEnd) / 2;
  return rangeStart + (index / (categoryCount - 1)) * (rangeEnd - rangeStart);
}

export function clusteredCategoryBand(
  categoryIndex: number,
  categoryCount: number,
  seriesIndex: number,
  seriesCount: number,
  rangeStart: number,
  rangeEnd: number,
  occupiedRatio = 0.72,
): CategoryBand {
  validateCategoryIndex(categoryIndex, categoryCount);
  validateCategoryIndex(seriesIndex, seriesCount);
  if (!Number.isFinite(rangeStart) || !Number.isFinite(rangeEnd)) throw new TypeError('Chart coordinates must be finite');
  if (!Number.isFinite(occupiedRatio) || occupiedRatio <= 0 || occupiedRatio > 1) throw new RangeError('Occupied ratio must be in the interval (0, 1]');

  const categorySize = (rangeEnd - rangeStart) / categoryCount;
  const clusterSize = categorySize * occupiedRatio;
  const bandSize = clusterSize / seriesCount;
  return {
    start: rangeStart + categoryIndex * categorySize + (categorySize - clusterSize) / 2 + seriesIndex * bandSize,
    size: bandSize,
  };
}

function validateCategoryIndex(index: number, count: number): void {
  if (!Number.isInteger(count) || count < 1) throw new RangeError('Category count must be a positive integer');
  if (!Number.isInteger(index) || index < 0 || index >= count) throw new RangeError(`Category index ${index} is outside the axis`);
}
