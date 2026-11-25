import { bool, component, number, text, token, valueData } from './shared.ts';

export function sliderValueAllowed(p: Readonly<Record<string, unknown>>, value: number): boolean {
  const min = (p.min ?? 0) as number;
  const max = (p.max ?? 100) as number;
  const step = (p.step ?? 1) as number;
  if (!Number.isFinite(value) || value < min || value > max) return false;
  if (p.dots) return value === min || value === max || Object.keys(p.marks ?? {}).some((key) => Number(key) === value);
  const steps = (value - min) / step;
  return Math.abs(steps - Math.round(steps)) <= 1e-7;
}

export const sliderRule = component('Slider', {
  min: number, max: number, step: number, value: number, defaultValue: number, disabled: bool, ariaLabel: text,
  marks: { kind: 'record', value: text }, dots: bool, included: bool, reverse: bool,
  orientation: token('horizontal', 'vertical'),
}, {
  defaults: { min: 0, max: 100, step: 1, dots: false, included: true, reverse: false, orientation: 'horizontal' },
  events: { 'ui.change': valueData(number) },
  validateEvent: (_event, data, p) => sliderValueAllowed(p, data.value as number) ? null : 'invalid slider value',
  validate(p) {
    const min = p.min as number; const max = p.max as number; const step = p.step as number;
    if (max <= min || step <= 0 || !Number.isFinite((max - min) / step)) return 'invalid slider interval';
    const count = (max - min) / step;
    if (Math.abs(count - Math.round(count)) > 1e-7) return 'step must divide the interval';
    const marks = Object.keys(p.marks ?? {});
    if (marks.some((key) => !key.trim() || !Number.isFinite(Number(key)) || Number(key) < min || Number(key) > max)
      || !uniqueMarks(marks)) return 'invalid slider marks';
    return ['value', 'defaultValue'].some((field) => p[field] !== undefined && !sliderValueAllowed(p, p[field] as number))
      ? 'invalid slider value' : null;
  },
  prepare(p) {
    if (p.defaultValue !== undefined) return p;
    const min = p.min as number; const max = p.max as number; const step = p.step as number;
    const zero = Math.max(min, Math.min(max, 0));
    const lower = Math.max(min, min + Math.floor((zero - min) / step) * step);
    const upper = Math.min(max, lower + step);
    const candidates = p.dots ? [min, max, ...Object.keys(p.marks ?? {}).map(Number)] : [lower, upper];
    candidates.sort((a, b) => Math.abs(a) - Math.abs(b) || a - b);
    return { ...p, defaultValue: candidates[0] };
  },
});

function uniqueMarks(keys: string[]): boolean { return new Set(keys.map(Number)).size === keys.length; }
