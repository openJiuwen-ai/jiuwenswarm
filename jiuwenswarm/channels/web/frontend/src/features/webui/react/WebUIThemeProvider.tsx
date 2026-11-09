import { useLayoutEffect, useState, type ReactNode } from 'react';
import { ConfigProvider, theme, type ThemeConfig } from 'antd';
import enUS from 'antd/locale/en_US.js';
import zhCN from 'antd/locale/zh_CN.js';
import type { Locale } from 'antd/es/locale';
import { useTranslation } from 'react-i18next';

const colorTokens = {
  colorPrimary: '--color-action-primary',
  colorPrimaryHover: '--color-action-primary-hover',
  colorPrimaryBg: '--color-action-primary-subtle',
  colorSuccess: '--color-feedback-success',
  colorWarning: '--color-feedback-warning',
  colorError: '--color-feedback-danger',
  colorInfo: '--color-action-primary',
  colorText: '--color-text-primary',
  colorTextSecondary: '--color-text-secondary',
  colorTextTertiary: '--color-text-tertiary',
  colorTextQuaternary: '--color-text-placeholder',
  colorTextDisabled: '--color-text-disabled',
  colorTextLightSolid: '--color-text-inverse',
  colorLink: '--color-text-link',
  colorBorder: '--color-border-default',
  colorBorderSecondary: '--color-border-default',
  colorBgBase: '--color-surface-page',
  colorBgContainer: '--color-surface-card',
  colorBgElevated: '--color-surface-popover',
  colorBgLayout: '--color-surface-page',
  colorBgContainerDisabled: '--color-surface-muted',
} as const;

function unwrapLocale(locale: Locale | { default: Locale }): Locale {
  return 'default' in locale ? locale.default : locale;
}

/** Resolve seed colors before passing them to Ant Design's palette algorithm. */
export function resolveWebUITheme(styles: Pick<CSSStyleDeclaration, 'getPropertyValue'>, dark: boolean): ThemeConfig {
  const token: NonNullable<ThemeConfig['token']> = {};
  for (const [name, variable] of Object.entries(colorTokens)) {
    const value = styles.getPropertyValue(variable).trim();
    if (value) token[name as keyof typeof colorTokens] = value;
  }
  const fontFamily = styles.getPropertyValue('--font-body').trim();
  if (fontFamily) token.fontFamily = fontFamily;
  for (const [name, variable] of [
    ['fontSize', '--font-size-base'],
    ['fontSizeSM', '--font-size-sm'],
    ['fontSizeLG', '--font-size-lg'],
    ['borderRadius', '--radius-md'],
  ] as const) {
    const value = Number.parseFloat(styles.getPropertyValue(variable));
    if (Number.isFinite(value) && value > 0) token[name] = value;
  }
  const textHoverBg = styles.getPropertyValue('--color-action-secondary').trim();
  return {
    algorithm: dark ? theme.darkAlgorithm : theme.defaultAlgorithm,
    token,
    components: {
      Button: {
        controlHeight: 32,
        contentFontSize: 14,
        paddingInline: 16,
        iconGap: 8,
        borderRadius: 8,
        defaultShadow: 'none',
        primaryShadow: 'none',
        dangerShadow: 'none',
        ...(textHoverBg ? { textHoverBg } : {}),
      },
    },
  };
}

/** Keep library tokens and built-in labels within the WebUI subtree. */
export function WebUIThemeProvider({ children }: { children: ReactNode }) {
  const { i18n } = useTranslation();
  const [config, setConfig] = useState<ThemeConfig>({});
  useLayoutEffect(() => {
    const element = document.documentElement;
    const update = () => setConfig(resolveWebUITheme(
      getComputedStyle(element), element.dataset.colorMode === 'dark',
    ));
    update();
    const observer = new MutationObserver(update);
    observer.observe(element, {
      attributes: true,
      // Appearance contributions and the application background change the theme variables too.
      attributeFilter: ['data-theme', 'data-color-mode', 'class', 'style', 'data-webui-appearance', 'data-webui-background'],
    });
    return () => observer.disconnect();
  }, []);
  const language = i18n.resolvedLanguage ?? i18n.language ?? 'zh';
  return (
    <ConfigProvider theme={config} locale={unwrapLocale(language.startsWith('zh') ? zhCN : enUS)}>
      {children}
    </ConfigProvider>
  );
}
