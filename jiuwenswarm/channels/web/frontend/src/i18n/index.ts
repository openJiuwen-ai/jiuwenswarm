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
    fallbackLng: 'zh',
    supportedLngs: ['zh', 'en'],
    interpolation: {
      escapeValue: false,
    },
    detection: {
      // 未手动选择过语言时默认中文（与后端 preferred_language 默认值一致），
      // 不再跟随 navigator（桌面 WebView2 常为 en-US，导致启动初期显示英文）。
      order: ['localStorage'],
      caches: ['localStorage'],
    },
  });

export default i18n;
