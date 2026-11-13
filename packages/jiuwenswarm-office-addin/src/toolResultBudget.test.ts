import { describe, expect, it } from 'vitest';
import { fitToolResultJson } from './toolResultBudget';

// Shaped like verify_slides on a dense slide: 45 boxes plus 35 small-text warnings
// serialized to well over the 9500-character budget in a live session.
function denseVerifySlidesResult(): Record<string, unknown> {
  const shapes = Array.from({ length: 45 }, (_, i) => ({ id: String(i), name: `shape-${i}`, left: 36, top: 30 + i, w: 190, h: 22 }));
  const smallText = Array.from({ length: 35 }, (_, i) => ({
    shape: `shape-${i}`,
    font_size: 11,
    message: `Text in 'shape-${i}' is 11 pt (below the 14 pt body floor). Source lines, captions and footnotes may stay at 10–12 pt; do not enlarge them.`,
  }));
  return {
    success: true,
    result: {
      slides: [{
        slideId: '302#449478236',
        shapes,
        overlaps: [],
        coverage: { coverage_ratio: 0.8 },
        small_text_warnings: smallText,
        sibling_warnings: [{ size: '190x58', shapes: ['n1', 'n2'], fills: ['#FFFFFF', '#F7F7F7'], message: '2 same-sized shapes differ in fill.' }],
      }],
    },
  };
}

describe('fitToolResultJson', () => {
  it('returns small results unchanged', () => {
    const value = { success: true, result: { slides: [{ slideId: 'a', shapes: [] }] } };
    expect(fitToolResultJson(value, 9500)).toBe(JSON.stringify(value));
  });

  it('keeps an oversized verify_slides result parseable and within budget', () => {
    const value = denseVerifySlidesResult();
    expect(JSON.stringify(value).length).toBeGreaterThan(9500);

    const text = fitToolResultJson(value, 9500);
    expect(text.length).toBeLessThanOrEqual(9500);
    const parsed = JSON.parse(text);
    expect(parsed.success).toBe(true);
    expect(parsed.truncated).toBe(true);

    const [slide] = parsed.result.slides;
    expect(slide.slideId).toBe('302#449478236');
    // The largest leaf list is trimmed first; the slide entry itself survives.
    expect(slide.small_text_warnings.length).toBeLessThan(35);
    expect(slide.small_text_warnings_omitted).toBe(35 - slide.small_text_warnings.length);
    expect(parsed.omitted).toContainEqual({
      path: 'result.slides[0].small_text_warnings',
      kept: slide.small_text_warnings.length,
      omitted: 35 - slide.small_text_warnings.length,
    });
    expect(slide.coverage).toEqual({ coverage_ratio: 0.8 });
  });

  it('does not mutate the caller value', () => {
    const value = denseVerifySlidesResult();
    const before = JSON.stringify(value);
    fitToolResultJson(value, 9500);
    expect(JSON.stringify(value)).toBe(before);
  });

  it('drops whole container entries only after their lists are exhausted', () => {
    const slides = Array.from({ length: 3 }, (_, i) => ({ slideId: `s${i}`, shapes: Array.from({ length: 20 }, (_, j) => ({ name: `x${j}`, left: j })) }));
    const text = fitToolResultJson({ success: true, result: { slides } }, 400);
    const parsed = JSON.parse(text);
    expect(text.length).toBeLessThanOrEqual(400);
    for (const slide of parsed.result.slides) expect(slide.shapes).toEqual([]);
  });

  it('falls back to a valid error object when nothing can be trimmed', () => {
    const text = fitToolResultJson({ success: true, result: { note: 'x'.repeat(20000) } }, 9500);
    const parsed = JSON.parse(text);
    expect(parsed).toMatchObject({ success: true, truncated: true });
    expect(parsed.error).toContain('20');
  });
});
