# Frontend development rules

## Scope

These rules apply only to newly added or modified code.

Do not trigger a repository-wide scan or migration solely because of these rules. When a feature changes an existing file, apply these rules to the touched code. Plan large-scale legacy cleanup as a separate, explicitly scoped task.

## Browser compatibility

- Chrome/Chromium 107 is the minimum supported browser baseline. Newly added or modified HTML, CSS, and JavaScript must render and run correctly in Chrome 107 and later.

## Formatting

- Follow `.prettierrc.cjs`.
- Do not introduce formatting conventions that conflict with Prettier.

## Settings UI consistency

These rules apply to every component rendered inside the Settings page, including components outside `src/features/settings/` (for example, `src/components/PersonalContext/`). Follow the scope rules above; do not migrate unrelated legacy code.

- Before adding or changing a settings module, read `src/features/settings/components/SettingRow.tsx`, `SettingRow.css`, `SettingsSection.tsx`, `SettingsSection.css`, and one existing module with the same interaction pattern. Use these shared components as the visual contract.
- For registered configuration fields, use the existing declarative `switch`, `select`, or `input` item definitions and `SettingItemRenderer`. Use `component: 'custom'` for business-specific data or interactions that the declarative items cannot express; a custom renderer is not an exemption from the shared layout.
- In custom renderers, use `SettingRow` for ordinary label/description/control rows. Use its `title`, `description`, `children`, and `subSettings` slots rather than recreating row markup or copying its font sizes, spacing, borders, and responsive CSS into module styles.
- Let `SettingsPageLayout` own the page title, content padding, and page scrolling. Embedded panels must not add another page title, page-level padding, fixed/full-page height, or page scroll container. A bounded business list may own its own scrolling when the interaction requires it.
- Let `SettingsSection` own section grouping through the module definition. For a custom component that owns its own card or list boundary, use `separatedRows: true` to avoid a second enclosing card border. Do not nest decorative cards around ordinary settings rows.
- Reuse the existing shared controls and semantic theme tokens. Do not override shared row typography, dimensions, or colors merely to give one module its own appearance. Business-specific layouts (for example, model lists or authorization panels) may use different structures while maintaining the Settings page's shared typography, spacing, alignment, colors, borders, corner radii, and control styling. Change shared components only when the requirement applies to their other consumers too.
- Keep added or changed user-visible text, including accessible labels, in both Chinese and English locale resources. Follow the inherited `data-testid` rules for touched elements.
- Before completing a settings UI change, compare it with an existing settings module in the browser. Check title/content alignment, row typography, grouping/borders, and narrow-screen layout; check expanded and collapsed states when affected. For theme changes, also check light and dark appearances. Report any states that could not be verified. Run `npm run build` and `git diff --check`; do not treat a successful build as visual verification.

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
