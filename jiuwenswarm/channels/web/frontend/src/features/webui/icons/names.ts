import whitelist from './whitelist.json';

/** Shared protocol names without importing React or icon artwork into the catalog. */
export const WEBUI_ICON_NAMES: readonly string[] = Object.freeze([
  ...Object.keys(whitelist.icons),
  ...Object.keys(whitelist.aliases),
]);
