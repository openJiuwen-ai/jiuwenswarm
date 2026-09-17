/** Mirrors ``jiuwenswarm.common.local_env_config.is_enterprise`` on the frontend. */
function readEdition(): string {
  const injected = String(window.__JIUWENSWARM_EDITION__ ?? "").trim().toLowerCase();
  if (injected && !injected.startsWith("__")) {
    return injected;
  }
  return String(import.meta.env.VITE_JIUWENSWARM_EDITION ?? "").trim().toLowerCase();
}

export const isEnterprise = (): boolean => readEdition() === "enterprise";

export const ENTERPRISE_HIDDEN_NAV_ITEMS = [
  "channels",
  "agents",
  "teams",
  "extensions",
  "configpanel",
  "browserpanel",
  "updatepanel",
] as const;
