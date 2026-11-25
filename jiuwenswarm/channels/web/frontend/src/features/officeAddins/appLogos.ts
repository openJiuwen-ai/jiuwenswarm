import { FileText, Presentation, Sheet, type LucideIcon } from 'lucide-react';
import excelLogo from '../../assets/office-addins/excel_48x1.svg';
import powerpointLogo from '../../assets/office-addins/powerpoint_48x1.svg';
import wordLogo from '../../assets/office-addins/word_48x1.svg';
import type { AppId } from './officeAddins';

/**
 * Official host logos, shown exactly as shipped: never recolored, dimmed, cropped or badged.
 * The WPS apps show a plain icon of their document kind.
 */
export const APP_LOGOS: Record<AppId, string | LucideIcon> = {
  word: wordLogo,
  excel: excelLogo,
  powerpoint: powerpointLogo,
  'wps-writer': FileText,
  'wps-spreadsheet': Sheet,
  'wps-presentation': Presentation,
};
