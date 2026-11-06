import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';
import LanguageDetector from 'i18next-browser-languagedetector';
import zh from './locales/zh.json';
import en from './locales/en.json';

const resources = {
  zh: { translation: zh },
  en: { translation: en },
};

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: 'en',
    supportedLngs: ['zh', 'en'],
    interpolation: {
      escapeValue: false,
    },
    detection: {
      // Default to English while preserving the user's saved language choice.
      // Keep the UI independent of the browser's navigator language.
      order: ['localStorage'],
      caches: ['localStorage'],
    },
  });

export default i18n;
