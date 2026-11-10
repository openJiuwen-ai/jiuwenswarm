import { describe, expect, it } from 'vitest';

import contractTools from '@contract/tools.json';

import type { DocumentId } from './documentIdentity';
import {
  CONTRACT_APP_ID, isMutatingTool, presentationAttachDeclaration, supportedContractTools,
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
