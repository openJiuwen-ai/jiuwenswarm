/** Excel: its identity, and the contract of the ``office-excel`` app's package directory. */
import contractProfile from '@office/excel/profile.json';
import contractTools from '@office/excel/tools.json';

import { ToolContract, type OfficeApp } from './appContract';

export const EXCEL: OfficeApp = {
  platform: 'office',
  surface: 'workbook',
  host: 'Excel',
  clientApp: 'office-excel',
  appId: contractProfile.app_id,
  contract: new ToolContract(contractTools),
  flatResults: new Set(['get_cell_ranges', 'get_range_as_csv', 'set_cell_range', 'copy_paste_range']),
  // A sheet's cells read in one call run long; the industry add-in gives a spreadsheet tool 40,000 characters.
  resultChars: 40_000,
};
