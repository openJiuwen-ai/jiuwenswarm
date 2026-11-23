export {
  APPEARANCE_PARTS,
  APPEARANCE_TOKEN_NAMES,
  DEFAULT_SURFACE_OPACITY,
  MAX_HIGHLIGHT_COLORS,
  MAX_HOVER_MS,
  MAX_NAME_CHARS,
  MAX_WELCOME_CHARS,
  MIN_HOVER_MS,
  WEBUI_APPEARANCE_EVENT,
  WELCOME_MODES,
  localizedText,
  readAppearanceFrame,
  validColor,
  validImage,
  validateAppearance,
  validateColors,
  validateIdentity,
  validateMascot,
  validateWelcome,
  welcomeSuffix,
} from './appearance.ts';
export type {
  WebUIAppearanceChange,
  WebUIAppearancePart,
  WebUIAppearanceParts,
  WebUIAppearancePartValue,
  WebUIAppearanceSnapshot,
  WebUIColors,
  WebUIHighlightScope,
  WebUIIdentity,
  WebUILocalizedText,
  WebUIMascot,
  WebUIWelcome,
  WebUIWelcomeMode,
} from './appearance.ts';
export {
  SURFACE_BASE_VARIABLES,
  accentVariables,
  buildAppearanceCss,
  contrastRatio,
  hoverAccent,
  parseColor,
  readSurfaceBases,
  readableHighlight,
  readableOn,
  strongAccent,
  translucentVariables,
} from './derive.ts';
export type { RGB, SurfaceBases } from './derive.ts';
export { APPEARANCE_STYLE_ID, applyAppearance } from './applyAppearance.ts';
export {
  APPEARANCE_INSTANCE_KEY,
  APPEARANCE_READY_MS,
  APPEARANCE_SLOT_ID,
  createAppearancePreparation,
  readIdentityRecord,
} from './preparation.ts';
export type { AppearancePreparation, AppearancePreparationOptions, AppearanceTimers } from './preparation.ts';
