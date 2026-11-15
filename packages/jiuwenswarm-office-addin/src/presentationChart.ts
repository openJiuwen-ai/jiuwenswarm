/**
 * The chart builder of create_slide_chart and edit_built_chart: a declarative spec, checked against the tools'
 * ``input_schema`` before it gets here, as chart XML that follows the house standard (no legend, a deleted value
 * axis on bar forms, one accent, units in the number formats), and the slide shapes that place it. What the schema
 * cannot say (lengths that must agree, indexes in range) is refused here with a ``ChartSpecError``.
 */
type Spec = Record<string, unknown>;

export const CHART_NAMESPACE = 'http://schemas.openxmlformats.org/drawingml/2006/chart';
const A = 'http://schemas.openxmlformats.org/drawingml/2006/main';
const R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
/** Stands for the chart's relationship id in a built frame, until the slide's relationships give it one. */
export const CHART_RELATIONSHIP_PLACEHOLDER = '__CHART_RID__';
const GAP = 50;
const BASE = {
  accent: '1A6BB8', negative: 'E07B39', totals: '595959', neutralBar: 'BFBFBF', neutralLine: 'A6A6A6',
  hairline: 'C9C9C9', gridline: 'ECECEC', text: '1A1A1A', textSecondary: '595959', bg: 'FFFFFF',
  ramp: ['737373', '9E9E9E', 'C4C4C4', 'DCDCDC'], font: 'Calibri',
};
type Style = typeof BASE;

/** A frame's place on the slide, in EMU. */
export interface ChartPosition { x: number; y: number; w: number; h: number }

/** A spec the schema allows that the builder cannot draw. */
export class ChartSpecError extends Error {}

const esc = (value: unknown) => String(value ?? '').replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, '')
  .replace(/[&<>"]/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[char]!);
const color = (value: unknown, fallback: string) => String(value ?? fallback).replace(/^#/, '').toUpperCase();

function styleOf(spec: Spec): Style {
  const input = (spec.style ?? {}) as Spec;
  return { ...BASE,
    accent: color(input.accent, BASE.accent), negative: color(input.negative, BASE.negative),
    totals: color(input.totals, BASE.totals), neutralBar: color(input.neutral, BASE.neutralBar),
    neutralLine: color(input.neutral, BASE.neutralLine), text: color(input.text, BASE.text),
    textSecondary: color(input.text_secondary, BASE.textSecondary), bg: color(input.bg, BASE.bg),
    ramp: ((input.ramp as string[] | undefined) ?? BASE.ramp).map(item => color(item, BASE.ramp[0])),
    font: (input.font as string | undefined) ?? BASE.font };
}

const solid = (hex: string) => `<a:solidFill><a:srgbClr val="${hex}"/></a:solidFill>`;
const fill = (hex: string) => `<c:spPr>${solid(hex)}<a:ln><a:noFill/></a:ln></c:spPr>`;
const noFill = '<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>';
const runStyle = (size: number, hex: string, bold: boolean, style: Style) =>
  `<a:defRPr sz="${size}" b="${bold ? 1 : 0}">${solid(hex)}<a:latin typeface="${esc(style.font)}"/></a:defRPr>`;
const txPr = (size: number, hex: string, style: Style, bold = false) => '<c:txPr><a:bodyPr rot="0" vert="horz"/>'
  + `<a:lstStyle/><a:p><a:pPr>${runStyle(size, hex, bold, style)}</a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr>`;
const cats = (values: unknown[]) => `<c:cat><c:strLit><c:ptCount val="${values.length}"/>${values
  .map((value, at) => (value === '' ? '' : `<c:pt idx="${at}"><c:v>${esc(value)}</c:v></c:pt>`)).join('')}</c:strLit></c:cat>`;
const nums = (values: unknown[], tag = 'c:val') => `<${tag}><c:numLit><c:ptCount val="${values.length}"/>${values
  .map((value, at) => (value === null ? '' : `<c:pt idx="${at}"><c:v>${Number(value)}</c:v></c:pt>`)).join('')}`
  + `</c:numLit></${tag}>`;
const show = (yes: boolean) => `<c:showLegendKey val="0"/><c:showVal val="${yes ? 1 : 0}"/><c:showCatName val="0"/>`
  + '<c:showSerName val="0"/><c:showPercent val="0"/><c:showBubbleSize val="0"/>';
const hiddenLabels = () => `<c:dLbls>${show(false)}</c:dLbls>`;

function labels(style: Style, options: { format?: string; pos?: string; hex?: string; bold?: boolean;
  perPoint?: string } = {}): string {
  return `<c:dLbls>${options.perPoint ?? ''}`
    + (options.format ? `<c:numFmt formatCode="${esc(options.format)}" sourceLinked="0"/>` : '')
    + `<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr>${runStyle(1400, options.hex ?? style.text, options.bold ?? false,
      style)}</a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr>`
    + `${options.pos ? `<c:dLblPos val="${options.pos}"/>` : ''}${show(true)}</c:dLbls>`;
}

function richRun(text: unknown, size: number, hex: string, bold: boolean, style: Style): string {
  return `<a:r><a:rPr lang="en-US" sz="${size}" b="${bold ? 1 : 0}">${solid(hex)}<a:latin typeface="${esc(style.font)}"/>`
    + `</a:rPr><a:t>${esc(text)}</a:t></a:r>`;
}

function customLabel(index: number, text: unknown, style: Style,
  options: { hex?: string; bold?: boolean; pos?: string } = {}): string {
  const hex = options.hex ?? style.text;
  const bold = options.bold ?? false;
  return `<c:dLbl><c:idx val="${index}"/><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:pPr>${runStyle(1400, hex, bold,
    style)}</a:pPr>${richRun(text, 1400, hex, bold, style)}</a:p></c:rich></c:tx>`
    + `${options.pos ? `<c:dLblPos val="${options.pos}"/>` : ''}${show(true)}</c:dLbl>`;
}

const ser = (index: number, name: unknown, body: string) =>
  `<c:ser><c:idx val="${index}"/><c:order val="${index}"/><c:tx><c:v>${esc(name)}</c:v></c:tx>${body}</c:ser>`;

function title(spec: Spec, style: Style): string {
  if (!spec.title) return '<c:autoTitleDeleted val="1"/>';
  const line = (text: unknown, size: number, hex: string, bold: boolean) =>
    `<a:p><a:pPr algn="l">${runStyle(size, hex, bold, style)}</a:pPr>${richRun(text, size, hex, bold, style)}</a:p>`;
  return '<c:title><c:tx><c:rich><a:bodyPr rot="0" vert="horz"/><a:lstStyle/>'
    + line(spec.title, 1800, style.text, true)
    + (spec.subtitle ? line(spec.subtitle, 1300, style.textSecondary, false) : '')
    + '</c:rich></c:tx><c:overlay val="0"/></c:title>';
}

const catAxis = (id: number, cross: number, pos: string) => `<c:catAx><c:axId val="${id}"/><c:scaling>`
  + `<c:orientation val="minMax"/></c:scaling><c:delete val="0"/><c:axPos val="${pos}"/><c:majorTickMark val="none"/>`
  + `<c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/><c:crossAx val="${cross}"/></c:catAx>`;

function valueAxis(id: number, cross: number, pos: string, style: Style, construction: Spec, values: number[],
  options: { min?: unknown; max?: unknown; unit?: unknown; format?: string; title?: unknown; grid?: boolean;
    shown?: boolean } = {}): string {
  const visible = construction.value_axis === 'shown' || options.shown === true;
  const low = options.min == null ? Math.min(0, ...values) : Number(options.min);
  const high = options.max == null ? Math.max(...values) : Number(options.max);
  // Over a short span the host steps the axis by fractions, which a whole-number format shows as repeated labels.
  const wholeNumbers = options.format !== undefined && !options.format.replace(/"[^"]*"/g, '').includes('.');
  const unit = options.unit ?? (visible && wholeNumbers && high - low <= 10 ? 1 : undefined);
  const grid = options.grid || construction.gridlines === true;
  const axisTitle = options.title ? '<c:title><c:tx><c:rich><a:bodyPr rot="0" vert="horz"/><a:lstStyle/><a:p><a:pPr>'
    + `${runStyle(1400, style.textSecondary, false, style)}</a:pPr>`
    + `${richRun(options.title, 1400, style.textSecondary, false, style)}</a:p></c:rich></c:tx><c:overlay val="0"/>`
    + '</c:title>' : '';
  return `<c:valAx><c:axId val="${id}"/><c:scaling><c:orientation val="minMax"/>`
    + (options.max == null ? '' : `<c:max val="${Number(options.max)}"/>`)
    + (options.min == null ? '' : `<c:min val="${Number(options.min)}"/>`)
    + `</c:scaling><c:delete val="${visible ? 0 : 1}"/><c:axPos val="${pos}"/>`
    + (grid ? `<c:majorGridlines><c:spPr><a:ln w="9525">${solid(style.gridline)}</a:ln></c:spPr></c:majorGridlines>` : '')
    + axisTitle
    + (visible && options.format ? `<c:numFmt formatCode="${esc(options.format)}" sourceLinked="0"/>` : '')
    + `<c:majorTickMark val="none"/><c:minorTickMark val="none"/><c:tickLblPos val="${visible ? 'nextTo' : 'none'}"/>`
    + (visible ? `<c:spPr><a:ln w="9525">${solid(style.hairline)}</a:ln></c:spPr>${txPr(1400, style.textSecondary, style)}`
      : '')
    + `<c:crossAx val="${cross}"/>${unit == null ? '' : `<c:majorUnit val="${Number(unit)}"/>`}</c:valAx>`;
}

function legend(construction: Spec, hidden: number[], style: Style): string {
  if (!construction.legend || construction.legend === 'none') return '';
  const position = ({ right: 'r', top: 't', bottom: 'b' } as Record<string, string>)[String(construction.legend)];
  return `<c:legend><c:legendPos val="${position}"/>${hidden
    .map(index => `<c:legendEntry><c:idx val="${index}"/><c:delete val="1"/></c:legendEntry>`).join('')}`
    + `<c:overlay val="0"/>${txPr(1200, style.textSecondary, style)}</c:legend>`;
}

function wrap(spec: Spec, plot: string, style: Style, construction: Spec, hidden: number[] = [], manual = false): string {
  // A chart with growth arrows has a fixed plot area, so the arrows drawn over it land on the bar tops.
  const layout = manual ? '<c:layout><c:manualLayout><c:layoutTarget val="inner"/><c:xMode val="edge"/>'
    + '<c:yMode val="edge"/><c:x val="0.02"/><c:y val="0.18"/><c:w val="0.96"/><c:h val="0.7"/></c:manualLayout></c:layout>'
    : '<c:layout/>';
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?><c:chartSpace xmlns:c="${CHART_NAMESPACE}" `
    + `xmlns:a="${A}" xmlns:r="${R}"><c:style val="2"/><c:chart>${title(spec, style)}<c:plotArea>${layout}${plot}`
    + `${noFill}</c:plotArea>${legend(construction, hidden, style)}<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/>`
    + `</c:chart>${noFill}</c:chartSpace>`;
}

function niceMax(value: number): number {
  if (!(value > 0)) throw new ChartSpecError('cannot size the value axis: the largest total must be positive');
  const target = value * 1.25;
  const unit = 10 ** (Math.floor(Math.log10(target)) - 2);
  return Number((Math.ceil(target / unit) * unit).toPrecision(6));
}

/** A label the builder computes (a slope end, a waterfall total) as ``format`` shows ``value``. */
function renderedNumber(value: number, format: string): string {
  const sections = format.split(';');
  const section = sections[value < 0 ? 1 : 0] ?? sections[0];
  const match = /^([+−-])?(?:"([^"]*)")?([#,0]+(?:\.(0+))?)(?:"([^"]*)")?$/.exec(section);
  if (!match) throw new ChartSpecError(`the builder cannot compute labels in the number format "${format}"`);
  const digits = match[4]?.length ?? 0;
  const number = Math.abs(value).toLocaleString('en-US',
    { minimumFractionDigits: digits, maximumFractionDigits: digits, useGrouping: match[3].includes(',') });
  return `${value < 0 ? '−' : match[1] === '+' ? '+' : ''}${match[2] ?? ''}${number}${match[5] ?? ''}`;
}

function columnOrBar(spec: Spec, style: Style, construction: Spec): string {
  const inputCats = spec.categories as string[];
  const inputValues = spec.values as number[];
  if (inputCats.length !== inputValues.length) throw new ChartSpecError('values and categories must have the same length');
  const horizontal = spec.type === 'bar';
  // OOXML draws a bar chart's categories bottom up; the spec lists them top down.
  const categories = horizontal ? [...inputCats].reverse() : inputCats;
  const values = horizontal ? [...inputValues].reverse() : inputValues;
  const stored = (index: number) => (horizontal ? categories.length - 1 - index : index);
  const accent = spec.accent_index as number | undefined;
  if (accent !== undefined && accent >= inputCats.length) throw new ChartSpecError('accent_index is out of range');
  const forecast = spec.forecast_from as number | undefined;
  if (forecast !== undefined && forecast >= categories.length) {
    throw new ChartSpecError(`forecast_from ${forecast} is out of range for ${categories.length} categories`);
  }
  const points = categories.map((_category, at) => {
    if (accent !== undefined && at === stored(accent)) {
      return `<c:dPt><c:idx val="${at}"/><c:bubble3D val="0"/>${fill(style.accent)}</c:dPt>`;
    }
    if (forecast === undefined || at < forecast) return '';
    return `<c:dPt><c:idx val="${at}"/><c:bubble3D val="0"/><c:spPr><a:pattFill prst="ltUpDiag"><a:fgClr><a:srgbClr `
      + `val="${style.neutralBar}"/></a:fgClr><a:bgClr><a:srgbClr val="${style.bg}"/></a:bgClr></a:pattFill><a:ln>`
      + '<a:noFill/></a:ln></c:spPr></c:dPt>';
  }).join('');
  const format = (spec.number_format as string | undefined) ?? '#,##0';
  const growth = ((spec.growth_arrows as unknown[] | undefined) ?? []).length > 0;
  const seen = new Set<number>();
  const perPoint = ((spec.point_labels as Spec[] | undefined) ?? []).map(entry => {
    const index = entry.index as number;
    if (index >= inputCats.length) throw new ChartSpecError(`point_labels index ${index} is out of range`);
    if (seen.has(index)) throw new ChartSpecError(`point_labels has two entries at index ${index}`);
    seen.add(index);
    return customLabel(stored(index), entry.text, style, { bold: true, pos: 'outEnd' });
  }).join('');
  const series = ser(0, 'Values', fill(style.neutralBar) + points
    + labels(style, { format, pos: 'outEnd', bold: true, perPoint }) + cats(categories) + nums(values));
  const plot = `<c:barChart><c:barDir val="${horizontal ? 'bar' : 'col'}"/><c:grouping val="clustered"/>`
    + `<c:varyColors val="0"/>${series}<c:gapWidth val="${GAP}"/><c:axId val="1"/><c:axId val="2"/></c:barChart>`
    + catAxis(1, 2, horizontal ? 'l' : 'b')
    + valueAxis(2, 1, horizontal ? 'b' : 'l', style, construction, values,
      { min: 0, max: growth ? niceMax(Math.max(...values)) : undefined, format });
  return wrap(spec, plot, style, construction, [], growth);
}

function line(spec: Spec, style: Style, construction: Spec): string {
  const categories = spec.categories as string[];
  const inputSeries = spec.series as Spec[];
  const forecast = spec.forecast_from as number | undefined;
  if (forecast !== undefined && forecast >= categories.length) {
    throw new ChartSpecError(`forecast_from ${forecast} is out of range for ${categories.length} categories`);
  }
  const hidden: number[] = [];
  const format = (spec.number_format as string | undefined) ?? '#,##0';
  const seriesXml = inputSeries.flatMap((item, seriesIndex) => {
    const name = String(item.name);
    const values = item.values as (number | null)[];
    if (values.length !== categories.length) throw new ChartSpecError(`series "${name}" must have one value per category`);
    const hex = item.accent ? style.accent : style.neutralLine;
    const width = item.accent ? 28575 : 19050;
    const first = values.findIndex(value => value !== null);
    const last = values.reduce<number>((found, value, at) => (value === null ? found : at), 0);
    const pointLabels = (item.point_labels as Spec[] | undefined)
      ?? [...(item.start_label ? [{ index: first, text: item.start_label }] : []),
        { index: last, text: item.end_label ?? item.name }];
    const seen = new Set<number>();
    for (const label of pointLabels) {
      const index = label.index as number;
      if (index < 0 || index >= values.length) {
        throw new ChartSpecError(`series "${name}" point_labels index ${index} is out of range`);
      }
      if (values[index] === null) throw new ChartSpecError(`series "${name}" point_labels index ${index} is a null point`);
      if (seen.has(index)) throw new ChartSpecError(`series "${name}" has two point_labels at index ${index}`);
      seen.add(index);
    }
    const labelXml = (accept: (index: number) => boolean) => `<c:dLbls>${pointLabels
      .filter(label => accept(label.index as number)).map(label => {
        const index = label.index as number;
        return customLabel(index, label.text, style,
          { hex, bold: Boolean(item.accent), pos: index === first ? 'l' : index === last ? 'r' : 't' });
      }).join('')}${show(false)}</c:dLbls>`;
    const one = (index: number, seriesName: unknown, dashed: boolean, shown: unknown[], dataLabels: string) =>
      ser(index, seriesName, `<c:spPr><a:ln w="${width}">${solid(hex)}${dashed ? '<a:prstDash val="sysDash"/>' : ''}`
        + `</a:ln></c:spPr><c:marker><c:symbol val="none"/></c:marker>${dataLabels}${cats(categories)}${nums(shown)}`
        + '<c:smooth val="0"/>');
    if (forecast === undefined) return [one(seriesIndex, item.name, false, values, labelXml(() => true))];
    // The forecast is a second, dashed series that starts at the last actual point; the legend leaves it out.
    hidden.push(inputSeries.length + seriesIndex);
    return [
      one(seriesIndex, item.name, false, values.map((value, at) => (at < forecast ? value : null)),
        labelXml(at => at < forecast)),
      one(inputSeries.length + seriesIndex, `${name} (forecast)`, true,
        values.map((value, at) => (at >= forecast - 1 ? value : null)), labelXml(at => at >= forecast)),
    ];
  }).join('');
  const axis = (spec.axis ?? {}) as Spec;
  const plotted = inputSeries.flatMap(item => (item.values as (number | null)[]).filter(value => value !== null));
  return wrap(spec, `<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>${seriesXml}<c:marker val="0"/>`
    + `<c:axId val="1"/><c:axId val="2"/></c:lineChart>${catAxis(1, 2, 'b')}${valueAxis(2, 1, 'l', style, construction,
      plotted, { min: axis.min, max: axis.max, unit: axis.unit, format, grid: spec.gridlines === true, shown: true })}`,
  style, construction, hidden);
}

function slope(spec: Spec, style: Style, construction: Spec): string {
  const items = spec.items as Spec[];
  const format = (spec.number_format as string | undefined) ?? '#,##0';
  const values = items.flatMap(item => [item.start as number, item.end as number]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const pad = Math.max((max - min) * 0.1, 1);
  const xml = items.map((item, at) => {
    const hex = item.accent ? style.accent : style.neutralLine;
    const bold = Boolean(item.accent);
    const name = String(item.name);
    return ser(at, item.name, `<c:spPr><a:ln w="${item.accent ? 28575 : 19050}">${solid(hex)}</a:ln></c:spPr>`
      + `<c:marker><c:symbol val="circle"/><c:size val="5"/><c:spPr>${solid(hex)}<a:ln><a:noFill/></a:ln></c:spPr>`
      + `</c:marker><c:dLbls>${customLabel(0, `${name} ${renderedNumber(item.start as number, format)}`, style,
        { hex, bold, pos: 'l' })}${customLabel(1, `${renderedNumber(item.end as number, format)} ${name}`, style,
        { hex, bold, pos: 'r' })}${show(false)}</c:dLbls>${cats([spec.start_label, spec.end_label])}`
      + `${nums([item.start, item.end])}<c:smooth val="0"/>`);
  }).join('');
  return wrap(spec, `<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>${xml}<c:marker val="1"/>`
    + `<c:axId val="1"/><c:axId val="2"/></c:lineChart>${catAxis(1, 2, 'b')}${valueAxis(2, 1, 'l', style, construction,
      values, { min: Math.floor(min - pad), max: Math.ceil(max + pad), format })}`, style, construction);
}

function stacked(spec: Spec, style: Style, construction: Spec): string {
  const categories = spec.categories as string[];
  const inputSeries = spec.series as Spec[];
  if (inputSeries.filter(item => item.accent).length > 1) throw new ChartSpecError('at most one series can carry the accent');
  const segmentFormat = (spec.segment_number_format as string | undefined) ?? '#,##0';
  const totalFormat = spec.totals_number_format as string;
  const ramp = ['A6A6A6', 'BFBFBF', 'D9D9D9', 'ECECEC'];
  let neutral = 0;
  const xml = inputSeries.map((item, at) => {
    const values = item.values as number[];
    if (values.length !== categories.length) {
      throw new ChartSpecError(`series "${String(item.name)}" must have one value per category`);
    }
    const hex = item.accent ? style.accent : ramp[Math.min(neutral++, ramp.length - 1)];
    const dataLabels = item.label_values === false ? hiddenLabels()
      : labels(style, { format: segmentFormat, pos: 'ctr', hex: item.accent ? 'FFFFFF' : style.text });
    return ser(at, item.name, fill(hex) + dataLabels + cats(categories) + nums(values));
  }).join('');
  const totals = categories.map((_category, at) => inputSeries
    .reduce((sum, item) => sum + (item.values as number[])[at], 0));
  const totalSeries = ser(inputSeries.length, 'Totals', noFill
    + labels(style, { format: totalFormat, pos: 'inBase', bold: true, hex: style.totals }) + cats(categories) + nums(totals));
  return wrap(spec, `<c:barChart><c:barDir val="col"/><c:grouping val="stacked"/><c:varyColors val="0"/>${xml}`
    + `${totalSeries}<c:gapWidth val="${GAP}"/><c:overlap val="100"/><c:axId val="1"/><c:axId val="2"/></c:barChart>`
    + `${catAxis(1, 2, 'b')}${valueAxis(2, 1, 'l', style, construction,
      totals, { min: 0, max: niceMax(Math.max(...totals)), format: segmentFormat })}`, style, construction);
}

function likert(spec: Spec, style: Style, construction: Spec): string {
  const levels = spec.levels as string[];
  // Rows read top down; OOXML draws them bottom up.
  const rows = [...(spec.rows as Spec[])].reverse();
  const minLabel = (spec.min_label_value as number | undefined) ?? 5;
  const format = (spec.number_format as string | undefined) ?? '0"%"';
  for (const row of rows) {
    if ((row.values as number[]).length !== levels.length) {
      throw new ChartSpecError(`row "${String(row.name)}" must have one value per level`);
    }
  }
  const xml = levels.map((level, at) => {
    const hex = at === levels.length - 1 ? style.accent : style.ramp[Math.min(at, style.ramp.length - 1)];
    const perPoint = rows.map((row, rowIndex) => ((row.values as number[])[at] < minLabel
      ? `<c:dLbl><c:idx val="${rowIndex}"/><c:delete val="1"/></c:dLbl>` : '')).join('');
    const labelHex = at < 2 || at === levels.length - 1 ? 'FFFFFF' : style.text;
    return ser(at, level, fill(hex) + labels(style, { format, pos: 'ctr', hex: labelHex, perPoint })
      + cats(rows.map(row => row.name)) + nums(rows.map(row => (row.values as number[])[at])));
  }).join('');
  return wrap(spec, `<c:barChart><c:barDir val="bar"/><c:grouping val="stacked"/><c:varyColors val="0"/>${xml}`
    + `<c:gapWidth val="${GAP}"/><c:overlap val="100"/><c:axId val="1"/><c:axId val="2"/></c:barChart>`
    + `${catAxis(1, 2, 'l')}${valueAxis(2, 1, 'b', style, construction, [0, 100],
      { min: 0, max: 100, format })}`,
  style, construction);
}

function waterfall(spec: Spec, style: Style, construction: Spec): string {
  const start = spec.start as { label: string; value: number };
  const deltas = spec.deltas as { label: string; value: number }[];
  const categories = [start.label, ...deltas.map(item => item.label), spec.end_label];
  // Stacked bars: an invisible base lifts each change to where the running total stands.
  const base: (number | null)[] = [0];
  const up: (number | null)[] = [null];
  const down: (number | null)[] = [null];
  let running = start.value;
  for (const { value } of deltas) {
    if (value >= 0) {
      base.push(running);
      up.push(value);
      down.push(null);
      running += value;
    } else {
      running += value;
      base.push(running);
      up.push(null);
      down.push(-value);
    }
    if (running < 0) throw new ChartSpecError('the running total falls below zero; a waterfall\'s bases must stay at 0 or more');
  }
  base.push(0);
  up.push(null);
  down.push(null);
  const total = [start.value, ...deltas.map(() => null), running];
  const tops = categories.map((_category, at) => (base[at] ?? 0) + (up[at] ?? 0) + (down[at] ?? 0) + (total[at] ?? 0));
  const totalFormat = spec.totals_number_format as string;
  const deltaFormat = (spec.delta_number_format as string | undefined) ?? '+#,##0;-#,##0';
  const custom = [
    customLabel(0, renderedNumber(start.value, totalFormat), style, { bold: true, hex: style.totals, pos: 'inBase' }),
    ...deltas.map((delta, at) => customLabel(at + 1, renderedNumber(delta.value, deltaFormat), style,
      { hex: delta.value >= 0 ? style.accent : style.negative, pos: 'inBase' })),
    customLabel(categories.length - 1, renderedNumber(running, totalFormat), style,
      { bold: true, hex: style.totals, pos: 'inBase' }),
  ].join('');
  const series = (index: number, name: string, look: string, values: unknown[], dataLabels = hiddenLabels()) =>
    ser(index, name, look + dataLabels + cats(categories) + nums(values));
  const xml = series(0, 'Base', noFill, base) + series(1, 'Increase', fill(style.accent), up)
    + series(2, 'Decrease', fill(style.negative), down) + series(3, 'Total', fill(style.totals), total)
    + series(4, 'Labels', noFill, categories.map(() => Math.max(...tops) * 0.01),
      `<c:dLbls>${custom}<c:dLblPos val="inBase"/>${show(true)}</c:dLbls>`);
  return wrap(spec, `<c:barChart><c:barDir val="col"/><c:grouping val="stacked"/><c:varyColors val="0"/>${xml}`
    + `<c:gapWidth val="${GAP}"/><c:overlap val="100"/><c:axId val="1"/><c:axId val="2"/></c:barChart>`
    + `${catAxis(1, 2, 'b')}${valueAxis(2, 1, 'l', style, construction,
      tops, { min: 0, max: niceMax(Math.max(...tops)), format: totalFormat })}`, style, construction);
}

function scatter(spec: Spec, style: Style, construction: Spec): string {
  const points = spec.points as { x: number; y: number }[];
  const accentIndex = spec.accent_point_index as number | undefined;
  if (accentIndex !== undefined && accentIndex >= points.length) {
    throw new ChartSpecError('accent_point_index is out of range');
  }
  const accent = accentIndex === undefined ? '' : `<c:dPt><c:idx val="${accentIndex}"/><c:marker><c:symbol val="circle"/>`
    + `<c:size val="7"/><c:spPr>${solid(style.accent)}<a:ln><a:noFill/></a:ln></c:spPr></c:marker><c:bubble3D val="0"/>`
    + '</c:dPt>';
  const body = '<c:spPr><a:ln><a:noFill/></a:ln></c:spPr><c:marker><c:symbol val="circle"/><c:size val="6"/><c:spPr>'
    + `${solid(style.neutralLine)}<a:ln><a:noFill/></a:ln></c:spPr></c:marker>${accent}${hiddenLabels()}`
    + `${nums(points.map(point => point.x), 'c:xVal')}${nums(points.map(point => point.y), 'c:yVal')}`;
  const axis = (id: number, cross: number, pos: string, input: Spec, values: number[]) => valueAxis(id, cross, pos,
    style, construction, values, { min: input.min ?? 0, max: input.max, unit: input.unit, format: (input.number_format as string | undefined)
      ?? '#,##0', title: input.title, shown: true });
  return wrap(spec, `<c:scatterChart><c:scatterStyle val="marker"/><c:varyColors val="0"/>${ser(0, 'Points', body)}`
    + `<c:axId val="1"/><c:axId val="2"/></c:scatterChart>${axis(1, 2, 'b', spec.x_axis as Spec, points.map(point => point.x))}`
    + axis(2, 1, 'l', spec.y_axis as Spec, points.map(point => point.y)), style, construction);
}

const FORMS: Record<string, (spec: Spec, style: Style, construction: Spec) => string> = {
  column: columnOrBar, bar: columnOrBar, line, slope, stacked_column: stacked, likert, waterfall, scatter,
};

/** The chart part of a spec of any form but ``big_number``, which is text shapes. */
export function buildChart(spec: Spec): string {
  if (spec.subtitle && !spec.title) throw new ChartSpecError('subtitle requires title');
  const form = FORMS[String(spec.type)];
  if (!form) throw new ChartSpecError(`${String(spec.type)} is not drawn as a native chart`);
  return form(spec, styleOf(spec), (spec.construction ?? {}) as Spec);
}

function textParagraph(text: unknown, size: number, hex: string, style: Style, bold = false, align = 'l'): string {
  return `<a:p><a:pPr algn="${align}"/><a:r><a:rPr lang="en-US" sz="${size}" b="${bold ? 1 : 0}" dirty="0">${solid(hex)}`
    + `<a:latin typeface="${esc(style.font)}"/></a:rPr><a:t>${esc(text)}</a:t></a:r></a:p>`;
}

function textShape(id: number, name: string, box: ChartPosition, paragraph: string, background?: string): string {
  return `<p:sp><p:nvSpPr><p:cNvPr id="${id}" name="${esc(name)}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr>`
    + `<a:xfrm><a:off x="${box.x}" y="${box.y}"/><a:ext cx="${box.w}" cy="${box.h}"/></a:xfrm><a:prstGeom prst="rect">`
    + `<a:avLst/></a:prstGeom>${background ? solid(background) : '<a:noFill/>'}</p:spPr><p:txBody>`
    + `<a:bodyPr wrap="square" anchor="ctr"><a:normAutofit/></a:bodyPr><a:lstStyle/>${paragraph}</p:txBody></p:sp>`;
}

function chartFrame(id: number, position: ChartPosition): string {
  return `<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="${id}" name="Chart"/><p:cNvGraphicFramePr/><p:nvPr/>`
    + `</p:nvGraphicFramePr><p:xfrm><a:off x="${position.x}" y="${position.y}"/><a:ext cx="${position.w}" `
    + `cy="${position.h}"/></p:xfrm><a:graphic><a:graphicData uri="${CHART_NAMESPACE}"><c:chart `
    + `xmlns:c="${CHART_NAMESPACE}" xmlns:r="${R}" r:id="${CHART_RELATIONSHIP_PLACEHOLDER}"/></a:graphicData></a:graphic>`
    + '</p:graphicFrame>';
}

function arrowLine(id: number, name: string, from: { x: number; y: number }, to: { x: number; y: number },
  hex: string): string {
  const flips = `${to.x < from.x ? ' flipH="1"' : ''}${to.y < from.y ? ' flipV="1"' : ''}`;
  return `<p:cxnSp><p:nvCxnSpPr><p:cNvPr id="${id}" name="${esc(name)}"/><p:cNvCxnSpPr/><p:nvPr/></p:nvCxnSpPr>`
    + `<p:spPr><a:xfrm${flips}><a:off x="${Math.min(from.x, to.x)}" y="${Math.min(from.y, to.y)}"/>`
    + `<a:ext cx="${Math.max(Math.abs(to.x - from.x), 1)}" cy="${Math.max(Math.abs(to.y - from.y), 1)}"/></a:xfrm>`
    + `<a:prstGeom prst="line"><a:avLst/></a:prstGeom><a:ln w="12700">${solid(hex)}`
    + '<a:tailEnd type="triangle" w="med" len="med"/></a:ln></p:spPr></p:cxnSp>';
}

/**
 * A column chart's growth arrows: per arrow a group named ``GrowthArrow f<frame id> <n>`` of a line from bar top to
 * bar top, lifted over the bars between, and its label. They sit over the frame at ``position``.
 */
export function buildGrowthArrows(spec: Spec, position: ChartPosition, firstId: number, frameId: number): string[] {
  const arrows = (spec.growth_arrows as Spec[] | undefined) ?? [];
  if (!arrows.length) return [];
  const style = styleOf(spec);
  let id = firstId;
  const values = spec.values as number[];
  const count = values.length;
  const maximum = niceMax(Math.max(...values));
  // The plot area of wrap's manual layout.
  const plotX = position.x + Math.round(position.w * 0.02);
  const plotY = position.y + Math.round(position.h * 0.18);
  const plotW = Math.round(position.w * 0.96);
  const plotH = Math.round(position.h * 0.7);
  const slot = plotW / count;
  const bar = slot / (1 + GAP / 100);
  const center = (index: number) => Math.round(plotX + (index + 0.5) * slot);
  const top = (index: number) => Math.round(plotY + plotH * (1 - Math.max(values[index] ?? 0, 0) / maximum));
  return arrows.map((arrow, arrowIndex) => {
    const from = (arrow.from as number | undefined) ?? arrowIndex;
    const to = (arrow.to as number | undefined) ?? from + 1;
    if (from >= count || to >= count || from === to) {
      throw new ChartSpecError(`growth_arrows[${arrowIndex}]: from and to must be two of the ${count} categories`);
    }
    const direction = to > from ? 1 : -1;
    const x1 = center(from) + Math.round(direction * bar / 2);
    const x2 = center(to) - Math.round(direction * bar / 2);
    let y1 = top(from) - 260000;
    let y2 = top(to) - 260000;
    for (let index = Math.min(from, to) + 1; index < Math.max(from, to); index += 1) {
      const lineY = y1 + (y2 - y1) * (center(index) - x1) / (x2 - x1);
      const required = top(index) - 260000;
      if (lineY > required) {
        const lift = Math.ceil(lineY - required);
        y1 -= lift;
        y2 -= lift;
      }
    }
    const labelX = Math.round((x1 + x2) / 2);
    const labelY = Math.round((y1 + y2) / 2);
    const labelW = Math.max(260000, Math.min(620000, Math.round(Math.abs(x2 - x1) * 0.7)));
    const labelH = 220000;
    const left = Math.min(x1, x2, labelX - labelW / 2);
    const upper = Math.min(y1, y2, labelY - labelH / 2);
    const right = Math.max(x1, x2, labelX + labelW / 2);
    const lower = Math.max(y1, y2, labelY + labelH / 2);
    const groupId = id++;
    const name = `GrowthArrow f${frameId} ${arrowIndex}`;
    const label = { x: Math.round(labelX - labelW / 2), y: Math.round(labelY - labelH / 2), w: labelW, h: labelH };
    return `<p:grpSp><p:nvGrpSpPr><p:cNvPr id="${groupId}" name="${name}"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>`
      + `<p:grpSpPr><a:xfrm><a:off x="${left}" y="${upper}"/><a:ext cx="${right - left}" cy="${lower - upper}"/>`
      + `<a:chOff x="${left}" y="${upper}"/><a:chExt cx="${right - left}" cy="${lower - upper}"/></a:xfrm></p:grpSpPr>`
      + arrowLine(id++, `${name} line`, { x: x1, y: y1 }, { x: x2, y: y2 }, style.textSecondary)
      + textShape(id++, `${name} label`, label, textParagraph(arrow.label, 1400, style.textSecondary, style, true, 'ctr'),
        style.bg)
      + '</p:grpSp>';
  });
}

/** ``box``, given for a 16:9 slide of 12192000 by 6858000 EMU, scaled to a slide ``slide.w`` by ``slide.h``. */
function onSlide(box: ChartPosition, slide: Pick<ChartPosition, 'w' | 'h'>): ChartPosition {
  const [across, down] = [slide.w / 12_192_000, slide.h / 6_858_000];
  return { x: Math.round(box.x * across), y: Math.round(box.y * down), w: Math.round(box.w * across),
    h: Math.round(box.h * down) };
}

/**
 * What create_slide_chart adds to a slide of size ``slide`` (EMU): the chart part and its frame (ids from
 * ``firstId``) plus a column chart's growth arrows, or for ``big_number`` text shapes and no chart part.
 */
export function buildChartShapes(spec: Spec, firstId: number, slide: Pick<ChartPosition, 'w' | 'h'>):
  { chartXml: string | null; shapeXmls: string[] } {
  if (spec.subtitle && !spec.title) throw new ChartSpecError('subtitle requires title');
  const input = spec.position as ChartPosition | undefined;
  if (spec.type === 'big_number') {
    const style = styleOf(spec);
    const box = input ?? onSlide({ x: 1000000, y: 1600000, w: 10192000, h: 3500000 }, slide);
    const line = (y: number, h: number) => ({ x: box.x, y, w: box.w, h });
    return { chartXml: null, shapeXmls: [
      textShape(firstId, 'Big number', line(box.y, Math.round(box.h * 0.74)),
        textParagraph(spec.title, 9600, style.text, style, true, 'ctr')),
      textShape(firstId + 1, 'Caption', line(box.y + Math.round(box.h * 0.61), 700000),
        textParagraph(spec.subtitle, 2400, style.textSecondary, style, false, 'ctr')),
      ...(spec.context ? [textShape(firstId + 2, 'Context', line(box.y + Math.round(box.h * 0.84), 500000),
        textParagraph(spec.context, 1400, style.textSecondary, style, false, 'ctr'))] : []),
    ] };
  }
  const position = input ?? onSlide({ x: 457200, y: 300000, w: 11277600, h: 5900000 }, slide);
  return { chartXml: buildChart(spec), shapeXmls: [chartFrame(firstId, position),
    ...(spec.type === 'column' ? buildGrowthArrows(spec, position, firstId + 1, firstId) : [])] };
}
