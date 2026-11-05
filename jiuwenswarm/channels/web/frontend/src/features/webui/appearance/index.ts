export {
  APPEARANCE_TOKEN_NAMES,
  DEFAULT_SURFACE_OPACITY,
  WEBUI_APPEARANCE_EVENT,
  readAppearanceFrame,
  validColor,
  validateAppearance,
} from './appearance.ts';
export type { WebUIAppearance, WebUIAppearanceFrame } from './appearance.ts';
export {
  SURFACE_BASE_VARIABLES,
  accentVariables,
  buildAppearanceCss,
  contrastRatio,
  hoverAccent,
  parseColor,
  readSurfaceBases,
  strongAccent,
  translucentVariables,
} from './derive.ts';
export type { RGB, SurfaceBases } from './derive.ts';
export { APPEARANCE_STYLE_ID, applyAppearance } from './applyAppearance.ts';
