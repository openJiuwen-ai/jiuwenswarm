import { describe, expect, it } from 'vitest';

import contractTools from '@contract/tools.json';

import type { DocumentId } from './documentIdentity';
import {
  checkInput, CONTRACT_APP_ID, isMutatingTool, presentationAttachDeclaration, supportedContractTools,
  type PresentationHost,
} from './presentationContract';
import { PRESENTATION_TOOL_NAMES, type PresentationToolName } from './presentationToolTypes';

/** An Office host that supports PowerPointApi up to ``maximum``. */
function powerPoint(maximum: string, worker = true): PresentationHost {
  const [maxMajor, maxMinor] = maximum.split('.').map(Number);
  return {
    isSetSupported: (_name, version) => {
      const [major, minor] = version.split('.').map(Number);
      return major < maxMajor || (major === maxMajor && minor <= maxMinor);
    },
    worker,
  };
}

const ALL_TOOLS: ReadonlySet<PresentationToolName> = new Set(PRESENTATION_TOOL_NAMES);

describe('the PowerPoint contract the pane shares with the backend', () => {
  it('names the same tools as the pane', () => {
    expect(CONTRACT_APP_ID).toBe('office_powerpoint');
    expect(contractTools.map(tool => tool.name).sort()).toEqual([...PRESENTATION_TOOL_NAMES].sort());
    expect(isMutatingTool('read_slide_image')).toBe(false);
    expect(isMutatingTool('presentation_apply_patch')).toBe(true);
  });

  it('declares only the tools the running PowerPoint can execute', () => {
    expect(supportedContractTools(powerPoint('1.4'), ALL_TOOLS)).toEqual(
      ['read_presentation_context', 'list_slide_shapes', 'verify_slides']);
    const withoutWorker = supportedContractTools(powerPoint('1.8', false), ALL_TOOLS);
    expect(withoutWorker).toContain('edit_slide_text');
    expect(withoutWorker).not.toContain('edit_slide_xml');
    expect(supportedContractTools(powerPoint('1.10'), ALL_TOOLS)).toEqual(PRESENTATION_TOOL_NAMES);
  });

  it('declares only the tools the pane implements', () => {
    const implemented = new Set<PresentationToolName>(
      ['read_presentation_context', 'read_slide_text', 'edit_slide_text', 'verify_slide_visual']);
    expect(supportedContractTools(powerPoint('1.10'), implemented)).toEqual(
      ['read_presentation_context', 'read_slide_text', 'edit_slide_text', 'verify_slide_visual']);
    expect(supportedContractTools(powerPoint('1.4'), implemented)).toEqual(['read_presentation_context']);
  });

  it('attaches the document under its GUID', () => {
    const documentId = '3f1c2a9e-7b4d-4e8a-9c21-5d6f7a8b9c0d' as DocumentId;
    expect(presentationAttachDeclaration(
      { documentId, displayName: 'Deck.pptx', host: powerPoint('1.4'), implemented: ALL_TOOLS })).toEqual({
      resource: { key: documentId, display_name: 'Deck.pptx', kind: 'presentation', host_vendor: 'microsoft' },
      capabilities: { supported_tools: ['read_presentation_context', 'list_slide_shapes', 'verify_slides'] },
    });
  });
});

describe('checkInput', () => {
  const COLUMN = { type: 'column', categories: ['Q1', 'Q2'], values: [1, 2] };
  const chart = (spec: object) => checkInput('create_slide_chart', { slide_id: '256#', spec });

  it('takes the shared fields of a chart spec only on the forms that list them, as the backend does', () => {
    expect(chart({ ...COLUMN, style: { accent: '1F4E79' }, position: { x: 0, y: 0, w: 914400, h: 914400 },
      construction: { legend: 'top' }, point_labels: [{ index: 0, text: 'peak' }] })).toBeUndefined();
    expect(chart({ type: 'big_number', title: '7%', subtitle: 'growth', style: { font: 'Calibri' } })).toBeUndefined();
    expect(chart({ ...COLUMN, style: { accent: 'blue' } })).toBe('spec.style.accent must match ^#?[0-9A-Fa-f]{6}$');
    expect(chart({ ...COLUMN, position: { x: 0, y: 0 } })).toBe('spec.position.w is required');
    expect(chart({ ...COLUMN, colour: '1F4E79' })).toMatch(/^spec\.colour is not a field here; the fields are type, /);
    expect(chart({ type: 'big_number', title: '7%', subtitle: 'growth', construction: { legend: 'top' } }))
      .toMatch(/^spec\.construction is not a field here/);
    expect(chart({ type: 'line', categories: ['a', 'b'], series: [{ name: 's', values: [1, 2] }],
      point_labels: [{ index: 0, text: 'x' }] })).toMatch(/^spec\.point_labels is not a field here/);
  });

  it('names the form a spec asks for, or that it asks for none', () => {
    expect(chart({ ...COLUMN, values: [1, -2] })).toBe('spec.values[1] must be at least 0');
    expect(chart({ ...COLUMN, type: 'pie' })).toMatch(/^spec must be one of its forms: type "column", type "bar", /);
    expect(chart({ type: 'line', categories: ['a', 'b'], series: [{ name: 's', values: [1, 'x'] }] }))
      .toBe('spec.series[0].values[1] must be a number; or spec.series[0].values[1] must be null');
  });

  it('counts characters, and takes no field the schema lacks however it is named', () => {
    const pair = (find: string) => checkInput('find_and_replace', { pairs: [{ find, replace: '' }] });
    expect(pair('😀'.repeat(300))).toBeUndefined();
    expect(pair('😀'.repeat(301))).toBe('pairs[0].find must have at most 300 characters');
    expect(checkInput('duplicate_slide', { slide_id: '256#', constructor: 1 })).toMatch(/^constructor is not a field/);
    expect(checkInput('duplicate_slide', {})).toBe('slide_id is required');
    expect(checkInput('duplicate_slide', 'x')).toBe('the input must be an object');
  });

  it('fails on a schema keyword it does not check, rather than let what it names through', () => {
    expect(() => checkInput('presentation_apply_patch', { design_system: { colors: { accent: '112233' } },
      base_structure_fingerprint: 'f', slides: [] })).toThrow('input_schema keyword propertyNames is not one the pane '
      + 'checks');
  });
});
