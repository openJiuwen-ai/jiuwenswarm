/**
 * Request budget for one equipment install RPC.
 *
 * Shared by every install entry point (expert, expert team, plugin): an
 * install walks two serial Hub requests plus a package download and
 * extraction, so the 15s default request timeout made the request look
 * failed while the server kept installing (#6426). The server caps one
 * install at 220s (`_EQUIPMENT_INSTALL_TIMEOUT_SECONDS`), so this leaves
 * 20s of round-trip margin and the server always answers first.
 *
 * It lives in `services/` because both `services/pluginPackagesApi` and
 * `features/agentManagement` need it; the constant must never depend on a
 * feature module.
 */
export const INSTALL_REQUEST_TIMEOUT_MS = 240_000;
