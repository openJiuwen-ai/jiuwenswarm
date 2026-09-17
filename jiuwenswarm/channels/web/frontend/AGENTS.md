# Frontend development rules

## Scope

These rules apply only to newly added or modified code.

Do not trigger a repository-wide scan or migration solely because of these rules. When a feature changes an existing file, apply these rules to the touched code. Plan large-scale legacy cleanup as a separate, explicitly scoped task.

## Browser compatibility

- Chrome/Chromium 107 is the minimum supported browser baseline. Newly added or modified HTML, CSS, and JavaScript must render and run correctly in Chrome 107 and later.

## Formatting

- Follow `.prettierrc.cjs`.
- Do not introduce formatting conventions that conflict with Prettier.

## Colors

- Do not hardcode product colors in business components, pages, or component styles, including hex values, `rgb()` / `rgba()`, or Tailwind palette classes.
- Use existing semantic theme tokens: CSS uses `var(--color-*)`; Tailwind uses semantic classes such as `bg-accent`, `text-text-link`, and `text-warn`.
- Define concrete color values only in theme token files. New tokens must be named for their role, not their hue.
- A2UI, Mermaid and chart palettes, logos, multicolor illustrations, image assets, SVG masks/clipping paths/transparent placeholders, and unavoidable browser or third-party constants may retain concrete colors.
- These exceptions must not be used for product UI backgrounds, borders, buttons, text, states, or interaction feedback.

## SVG

- New or modified static, single-color UI SVG assets must use `fill="currentColor"` and/or `stroke="currentColor"` on visible paths.
- Import those SVG assets through SVGR as React components, for example `import SettingsIcon from '../../assets/sidebar/config.svg?react';`.
- New or modified inline SVG must use `currentColor`; it does not need to be extracted into a separate asset solely for this rule.
- Do not load a themeable SVG with `currentColor` through `<img>`.
- Logos, multicolor illustrations, image resources, and SVGs that do not need theming may retain their original colors and use `<img>`.
