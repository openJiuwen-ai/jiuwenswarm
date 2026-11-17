/** Word: its identity, and the contract of the ``office-word`` app's package directory. */
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
