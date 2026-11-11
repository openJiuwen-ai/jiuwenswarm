/**
 * The PowerPoint tool contract, shared with the server: tools.json and
 * profile.json of the ``office-powerpoint`` app's package directory, which
 * the backend loads too (``@contract``, see vite.config.ts). The pane never
 * edits it; it only reports which of its tools it can run in the running
 * PowerPoint.
 */
import contractTools from '@contract/tools.json';
import contractProfile from '@contract/profile.json';

import type { DocumentId } from './documentIdentity';
import { PRESENTATION_TOOL_NAMES, type PresentationToolName } from './presentationToolTypes';

interface ContractTool {
  name: string;
  annotations: { mutating: boolean };
  host_requirements: { powerpoint_api: string; worker?: boolean };
}

const CONTRACT_TOOLS: readonly ContractTool[] = contractTools;
const TOOLS_BY_NAME = new Map(CONTRACT_TOOLS.map(tool => [tool.name, tool]));

/** The ``client_app`` of the pane's conversations, as the backend registers the app. */
export const POWERPOINT_CLIENT_APP = 'office-powerpoint';
export const CONTRACT_APP_ID: string = contractProfile.app_id;

export function isContractTool(name: string): name is PresentationToolName {
  return TOOLS_BY_NAME.has(name);
}

function contractTool(name: PresentationToolName): ContractTool {
  const tool = TOOLS_BY_NAME.get(name);
  if (!tool) throw new Error(`${name} is not part of the PowerPoint contract`);
  return tool;
}

export function isMutatingTool(name: PresentationToolName): boolean {
  return contractTool(name).annotations.mutating;
}

/** What the running Office host offers the contract's ``host_requirements``. */
export interface PresentationHost {
  isSetSupported(name: 'PowerPointApi', version: string): boolean;
  worker: boolean;
}

/** The tools that are in the contract, implemented by the pane's tool runtime, and supported by the host. */
export function supportedContractTools(
  host: PresentationHost, implemented: ReadonlySet<PresentationToolName>,
): PresentationToolName[] {
  return PRESENTATION_TOOL_NAMES.filter(name => {
    const requirements = TOOLS_BY_NAME.get(name)?.host_requirements;
    return requirements !== undefined && implemented.has(name)
      && host.isSetSupported('PowerPointApi', requirements.powerpoint_api) && (!requirements.worker || host.worker);
  });
}

/** What a presentation's task pane sends with ``client.attach``, besides the conversation. */
export interface PresentationAttachDeclaration {
  resource: { key: DocumentId; display_name: string; kind: 'presentation'; host_vendor: 'microsoft' };
  capabilities: { supported_tools: PresentationToolName[] };
}

export function presentationAttachDeclaration({ documentId, displayName, host, implemented }: {
  documentId: DocumentId; displayName: string; host: PresentationHost; implemented: ReadonlySet<PresentationToolName>;
}): PresentationAttachDeclaration {
  return {
    resource: { key: documentId, display_name: displayName, kind: 'presentation', host_vendor: 'microsoft' },
    capabilities: { supported_tools: supportedContractTools(host, implemented) },
  };
}
