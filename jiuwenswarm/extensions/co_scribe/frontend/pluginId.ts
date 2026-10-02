/**
 * The plugin's identity, in one place.
 *
 * ``extension.py`` declares the same string as ``plugin_id``; the manifest the
 * server publishes carries it, and the host looks the plugin up by it when it
 * has to know whether co-scribe is on (the document workbench renders in the
 * chat surface, which is not a nav page, so it has no contribution point yet).
 */
export const CO_SCRIBE_PLUGIN_ID = 'co-scribe';
