import i18n from '../../i18n';
import type { SupportedLocale } from './adapter';

export function getAgentManagementLocale(): SupportedLocale {
  return i18n.language?.startsWith('en') ? 'en' : 'zh';
}
