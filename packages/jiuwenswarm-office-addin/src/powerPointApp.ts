/** PowerPoint: its identity, and the contract of the ``office-powerpoint`` app's package directory. */
import contractProfile from '@office/powerpoint/profile.json';
import contractTools from '@office/powerpoint/tools.json';

import { ToolContract, type OfficeApp } from './appContract';

export const POWERPOINT: OfficeApp = {
  platform: 'microsoft',
  surface: 'presentation',
  host: 'PowerPoint',
  clientApp: 'office-powerpoint',
  appId: contractProfile.app_id,
  contract: new ToolContract(contractTools),
  flatResults: new Set(['read_presentation_context', 'verify_slide_visual']),
};

export const PRESENTATION_TOOL_NAMES = [
  'read_presentation_context',
  'list_slide_shapes',
  'edit_slide_chart',
  'create_slide_chart',
  'read_chart_style',
  'edit_built_chart',
  'duplicate_slide',
  'import_slides',
  'copy_image_between_slides',
  'edit_slide_master',
  'read_slide_text',
  'read_slide_image',
  'edit_slide_text',
  'edit_slide_xml',
  'verify_slides',
  'verify_slide_visual',
  'find_and_replace',
  'presentation_apply_patch',
] as const;

export type PresentationToolName = (typeof PRESENTATION_TOOL_NAMES)[number];
