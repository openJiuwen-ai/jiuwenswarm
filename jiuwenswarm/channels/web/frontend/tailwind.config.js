import typography from '@tailwindcss/typography';

/**
 * Normal utilities use the semantic color token directly. Opacity modifiers
 * such as `bg-secondary/30` use its RGB channels, which Chrome 107 supports.
 */
function color(token) {
  return ({ opacityValue = '1', opacityVariable }) => {
    if (opacityVariable) return `var(${token})`;
    return `rgba(var(${token}-rgb), ${opacityValue})`;
  };
}

/** Use only for semantic tokens whose base value already has transparency. */
function translucentColor(token) {
  return ({ opacityValue = '1', opacityVariable }) => {
    if (opacityVariable) return `var(${token})`;
    return `rgba(var(${token}-rgb), calc(var(${token}-alpha) * ${opacityValue}))`;
  };
}

/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // CSS variable based colors for JiuwenSwarm design system
        bg: {
          DEFAULT: color('--color-surface-page'),
          accent: color('--color-surface-page-accent'),
          elevated: color('--color-surface-elevated'),
          hover: color('--color-surface-hover'),
          muted: color('--color-surface-muted'),
          content: color('--color-surface-content'),
        },
        card: {
          DEFAULT: color('--color-surface-card'),
          foreground: color('--color-text-card'),
        },
        panel: {
          DEFAULT: color('--color-surface-panel'),
          strong: color('--color-surface-panel-strong'),
          hover: color('--color-surface-panel-hover'),
        },
        text: {
          DEFAULT: color('--color-text-primary'),
          strong: color('--color-text-strong'),
          muted: color('--color-text-secondary'),
          inverse: color('--color-text-inverse'),
          link: color('--color-text-link'),
        },
        border: {
          DEFAULT: color('--color-border-default'),
          strong: color('--color-border-strong'),
          hover: color('--color-border-hover'),
          accent: translucentColor('--color-border-accent'),
        },
        accent: {
          DEFAULT: color('--color-action-primary'),
          hover: color('--color-action-primary-hover'),
          subtle: color('--color-action-primary-subtle'),
          foreground: color('--color-action-primary-text'),
        },
        secondary: {
          DEFAULT: color('--color-action-secondary'),
          foreground: color('--color-action-secondary-text'),
        },
        control: {
          emphasis: color('--color-control-emphasis'),
          'emphasis-foreground': color('--color-control-emphasis-text'),
          'emphasis-hover': color('--color-control-emphasis-hover'),
          'emphasis-hover-strong': color('--color-control-emphasis-hover-strong'),
          ring: color('--color-control-ring'),
        },
        // Semantic colors
        ok: {
          DEFAULT: color('--color-feedback-success'),
          subtle: color('--color-feedback-success-subtle'),
        },
        warn: {
          DEFAULT: color('--color-feedback-warning'),
          subtle: color('--color-feedback-warning-subtle'),
        },
        danger: {
          DEFAULT: color('--color-feedback-danger'),
          subtle: color('--color-feedback-danger-subtle'),
        },
        info: color('--color-feedback-info'),
        cron: {
          running: color('--color-cron-running'),
          action: color('--color-cron-action'),
          'action-hover': color('--color-cron-action-hover'),
          'action-foreground': color('--color-cron-action-foreground'),
          'action-link': color('--color-cron-action-link'),
          'auto-managed-surface': color('--color-cron-auto-managed-surface'),
          'auto-managed-text': color('--color-cron-auto-managed-text'),
        },
        overlay: {
          'cron-dialog': color('--color-overlay-cron-dialog'),
          'cron-drawer': color('--color-overlay-cron-drawer'),
        },
        muted: {
          DEFAULT: color('--color-text-secondary'),
          foreground: color('--color-text-secondary'),
          strong: color('--color-text-tertiary'),
        },
      },
      fontFamily: {
        body: ['var(--font-body)'],
        display: ['var(--font-display)'],
        mono: ['var(--font-mono)'],
      },
      typography: {
        DEFAULT: {
          css: {
            '--tw-prose-body': 'var(--color-text-primary)',
            '--tw-prose-headings': 'var(--color-text-strong)',
            '--tw-prose-lead': 'var(--color-text-secondary)',
            '--tw-prose-links': 'var(--color-text-link)',
            '--tw-prose-bold': 'var(--color-text-strong)',
            '--tw-prose-counters': 'var(--color-text-secondary)',
            '--tw-prose-bullets': 'var(--color-text-secondary)',
            '--tw-prose-hr': 'var(--color-border-default)',
            '--tw-prose-quotes': 'var(--color-text-primary)',
            '--tw-prose-quote-borders': 'var(--color-border-strong)',
            '--tw-prose-captions': 'var(--color-text-secondary)',
            '--tw-prose-kbd': 'var(--color-text-primary)',
            '--tw-prose-kbd-shadows': 'var(--color-border-default)',
            '--tw-prose-code': 'var(--color-text-strong)',
            '--tw-prose-pre-code': 'var(--color-text-primary)',
            '--tw-prose-pre-bg': 'var(--color-surface-panel)',
            '--tw-prose-th-borders': 'var(--color-border-strong)',
            '--tw-prose-td-borders': 'var(--color-border-default)',
          },
        },
      },
      borderRadius: {
        sm: 'var(--radius-sm)',
        md: 'var(--radius-md)',
        lg: 'var(--radius-lg)',
        xl: 'var(--radius-xl)',
        full: 'var(--radius-full)',
      },
      boxShadow: {
        sm: 'var(--effect-shadow-sm)',
        md: 'var(--effect-shadow-md)',
        lg: 'var(--effect-shadow-lg)',
        xl: 'var(--effect-shadow-xl)',
        glow: 'var(--effect-shadow-glow)',
        focus: 'var(--effect-focus-ring)',
      },
      animation: {
        'pulse-slow': 'pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'cursor-blink': 'blink 1s step-end infinite',
        rise: 'rise 0.35s var(--ease-out) backwards',
        'fade-in': 'fade-in 0.2s  forwards',
        'scale-in': 'scale-in 0.2s var(--ease-out)',
        'glow-pulse': 'glow-pulse 2s  infinite',
        'stream-pulse': 'chatStreamPulse 1.5s  infinite',
      },
      keyframes: {
        blink: {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0' },
        },
        rise: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
        'scale-in': {
          from: { opacity: '0', transform: 'scale(0.95)' },
          to: { opacity: '1', transform: 'scale(1)' },
        },
      },
      spacing: {
        'shell-pad': 'var(--shell-pad)',
        'shell-gap': 'var(--shell-gap)',
        'shell-nav': 'var(--shell-nav-width)',
        'shell-topbar': 'var(--shell-topbar-height)',
      },
    },
  },
  plugins: [
    typography,
  ],
}
