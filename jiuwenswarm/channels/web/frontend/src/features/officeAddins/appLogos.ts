import excelLogo from '../../assets/office-addins/excel_48x1.svg';
import powerpointLogo from '../../assets/office-addins/powerpoint_48x1.svg';
import wordLogo from '../../assets/office-addins/word_48x1.svg';
import type { AppId } from './officeAddins';

/** Official host logos, shown exactly as shipped: never recolored, dimmed, cropped or badged. */
export const APP_LOGOS: Record<AppId, string> = {
  word: wordLogo,
  excel: excelLogo,
  powerpoint: powerpointLogo,
};
