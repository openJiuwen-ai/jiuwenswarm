import { DOMParser, onErrorStopParsing } from '@xmldom/xmldom';
import { describe, expect, it } from 'vitest';

import { buildChart, buildChartShapes, ChartSpecError } from './presentationChart';

const COLUMN = { type: 'column', categories: ['2024', '2025', '2026'], values: [10, 14, 18] };
const WIDE = { w: 12192000, h: 6858000 };

function wellFormed(xml: string): string {
  new DOMParser({ onError: onErrorStopParsing }).parseFromString(xml, 'application/xml');
  return xml;
}

describe('the chart builder', () => {
  it('draws every form as well-formed chart XML', () => {
    const specs = [
      { ...COLUMN, accent_index: 1, forecast_from: 2, title: 'Revenue', subtitle: 'per year' },
      { ...COLUMN, type: 'bar' },
      { type: 'line', categories: ['Q1', 'Q2'], series: [{ name: 'A & B', values: [1, null] }] },
      { type: 'slope', start_label: '2023', end_label: '2025', items: [{ name: 'A', start: 1, end: 2 },
        { name: 'B', start: 3, end: 1 }] },
      { type: 'stacked_column', categories: ['A', 'B'], totals_number_format: '#,##0" users"',
        series: [{ name: 'One', values: [2, 3] }, { name: 'Two', values: [4, 5], accent: true }] },
      { type: 'likert', levels: ['Low', 'High'], rows: [{ name: 'A', values: [3, 97] }, { name: 'B', values: [10, 90] }] },
      { type: 'waterfall', start: { label: 'Start', value: 10 }, deltas: [{ label: 'Gain', value: 5 },
        { label: 'Loss', value: -3 }], end_label: 'End', totals_number_format: '"$"#,##0' },
      { type: 'scatter', points: [{ x: 1, y: 2 }, { x: 3, y: 4 }], accent_point_index: 1,
        x_axis: { max: 5, unit: 1, title: 'x' }, y_axis: { max: 5, unit: 1 } },
    ];
    for (const spec of specs) expect(wellFormed(buildChart(spec))).toContain('<c:chartSpace');
  });

  it('marks the accent, hatches the forecast and fixes the plot area under growth arrows', () => {
    const spec = { ...COLUMN, accent_index: 1, forecast_from: 2, growth_arrows: [{ from: 0, to: 2, label: '+80%' }] };
    const built = buildChartShapes(spec, 10, WIDE);
    expect(built.chartXml).toContain('<c:dPt><c:idx val="1"/><c:bubble3D val="0"/><c:spPr><a:solidFill><a:srgbClr val="1A6BB8"/>');
    expect(built.chartXml).toContain('<c:dPt><c:idx val="2"/><c:bubble3D val="0"/><c:spPr><a:pattFill prst="ltUpDiag">');
    expect(built.chartXml).toContain('<c:manualLayout>');
    expect(built.shapeXmls[0]).toContain('<p:cNvPr id="10" name="Chart"/>');
    expect(built.shapeXmls[1]).toContain('<p:cNvPr id="11" name="GrowthArrow f10 0"/>');
    expect(built.shapeXmls.slice(1).join('')).toContain('<a:t>+80%</a:t>');
  });

  it('steps a short whole-number axis by one so no tick label repeats', () => {
    const line = (values: number[], extra: object = {}) => buildChart({ type: 'line', categories: ['Q1', 'Q2', 'Q3'],
      series: [{ name: 'Revenue', values }], ...extra });
    expect(line([1, 2, 3])).toContain('<c:majorUnit val="1"/>');
    expect(line([1, 2, 3], { axis: { unit: 0.5 } })).toContain('<c:majorUnit val="0.5"/>');
    expect(line([1, 2, 3], { number_format: '#,##0.0' })).not.toContain('<c:majorUnit');
    expect(line([10, 20, 30])).not.toContain('<c:majorUnit');
  });

  it('takes the deck style it is given', () => {
    const xml = buildChart({ ...COLUMN, accent_index: 0, style: { accent: '#c00000', font: 'Noto Sans' },
      construction: { legend: 'top', value_axis: 'shown', gridlines: true } });
    expect(xml).toContain('<a:srgbClr val="C00000"/>');
    expect(xml).toContain('typeface="Noto Sans"');
    expect(xml).toContain('<c:legendPos val="t"/>');
    expect(xml).toContain('<c:delete val="0"/><c:axPos val="l"/><c:majorGridlines>');
  });

  it('puts a horizontal bar\'s categories and point labels in display order, top first', () => {
    const xml = buildChart({ type: 'bar', categories: ['问界', '尚界', '智界'], values: [16.27, 3.15, 1.2],
      accent_index: 0, point_labels: [{ index: 0, text: '16.3 万辆' }] });
    expect(xml).toContain('<c:pt idx="0"><c:v>智界</c:v></c:pt><c:pt idx="1"><c:v>尚界</c:v></c:pt>');
    expect(xml).toContain('<c:dLbl><c:idx val="2"/>');
    expect(xml).toContain('<c:dPt><c:idx val="2"/>');
    expect(xml).toContain('<a:t>16.3 万辆</a:t>');
  });

  it('draws a line forecast as a dashed second series the legend leaves out', () => {
    const xml = buildChart({ type: 'line', categories: ['Q1', 'Q2', 'Q3'], forecast_from: 2,
      construction: { legend: 'right' }, series: [{ name: 'Revenue', values: [1, 2, 3], accent: true }] });
    expect(xml.match(/<c:ser>/g)).toHaveLength(2);
    expect(xml).toContain('<c:v>Revenue (forecast)</c:v>');
    expect(xml).toContain('<a:prstDash val="sysDash"/>');
    expect(xml).toContain('<c:legendEntry><c:idx val="1"/><c:delete val="1"/></c:legendEntry>');
  });

  it('computes the labels of slopes and waterfalls in their number format', () => {
    expect(buildChart({ type: 'slope', start_label: 'a', end_label: 'b', number_format: '0.0"%"',
      items: [{ name: 'A', start: 61, end: 65.25 }, { name: 'B', start: 1, end: 2 }] })).toContain('<a:t>A 61.0%</a:t>');
    const waterfall = buildChart({ type: 'waterfall', start: { label: 'Start', value: 1000 },
      deltas: [{ label: 'Loss', value: -300 }], end_label: 'End', totals_number_format: '"$"#,##0"M"' });
    expect(waterfall).toContain('<a:t>$1,000M</a:t>');
    expect(waterfall).toContain('<a:t>−300</a:t>');
    expect(waterfall).toContain('<a:t>$700M</a:t>');
  });

  it('draws a big number as text shapes, without a chart part', () => {
    const big = buildChartShapes({ type: 'big_number', title: '42%', subtitle: 'Growth', context: 'FY 2026' }, 20, WIDE);
    expect(big.chartXml).toBeNull();
    expect(big.shapeXmls.map(xml => /name="([^"]+)"/.exec(xml)![1])).toEqual(['Big number', 'Caption', 'Context']);
    expect(big.shapeXmls[2]).toContain('<p:cNvPr id="22"');
  });

  it('refuses what the schema allows but no chart can show', () => {
    const refusals: [object, RegExp][] = [
      [{ ...COLUMN, values: [1, 2] }, /same length/],
      [{ ...COLUMN, accent_index: 3 }, /accent_index/],
      [{ ...COLUMN, forecast_from: 3 }, /forecast_from 3/],
      [{ ...COLUMN, point_labels: [{ index: 0, text: 'a' }, { index: 0, text: 'b' }] }, /two entries at index 0/],
      [{ ...COLUMN, subtitle: 'no title' }, /subtitle requires title/],
      [{ type: 'line', categories: ['a', 'b'], series: [{ name: 's', values: [1, null],
        point_labels: [{ index: 1, text: 'x' }] }] }, /null point/],
      [{ type: 'stacked_column', categories: ['A'], totals_number_format: '0', series: [
        { name: 'a', values: [1], accent: true }, { name: 'b', values: [1], accent: true }] }, /one series/],
      [{ type: 'waterfall', start: { label: 'S', value: 2 }, deltas: [{ label: 'L', value: -3 }], end_label: 'E',
        totals_number_format: '#,##0' }, /below zero/],
      [{ type: 'waterfall', start: { label: 'S', value: 2 }, deltas: [{ label: 'L', value: 1 }], end_label: 'E',
        totals_number_format: '0.0E+00' }, /cannot compute labels/],
    ];
    for (const [spec, message] of refusals) expect(() => buildChart(spec as never)).toThrow(message);
    expect(() => buildChartShapes({ ...COLUMN, growth_arrows: [{ from: 1, to: 1, label: 'x' }] }, 1, WIDE))
      .toThrow(ChartSpecError);
  });
});
