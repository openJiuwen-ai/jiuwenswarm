/**
 * Word and WPS Writer: their identities, and the contract of the package directory both apps (``office-word``,
 * ``wps-writer``) load.
 */
import contractProfile from '@office/word/profile.json';
import contractTools from '@office/word/tools.json';

import { ToolContract, type OfficeApp } from './appContract';

export const WORD: OfficeApp = {
  platform: 'office',
  surface: 'document',
  host: 'Word',
  clientApp: 'office-word',
  appId: contractProfile.app_id,
  contract: new ToolContract(contractTools),
  // A section read or a document's text for a claim check runs long; the industry add-in gives Word 200,000.
  resultChars: 200_000,
};

export const WPS_WRITER: OfficeApp = { ...WORD, platform: 'wps', host: 'WPS Writer', clientApp: 'wps-writer' };
