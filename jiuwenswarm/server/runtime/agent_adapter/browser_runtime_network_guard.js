const INTERNAL_HOSTS = ["internal", "local", "lan", "home.arpa", "localhost"];
const METADATA_HOSTS = new Set([
  "169.254.169.254",
  "metadata",
  "metadata.google.internal",
]);
const PUBLIC_SUFFIX_ONLY_HOSTS = new Set([
  "app", "co.uk", "com", "dev", "github.io", "gov", "io", "net", "org", "test",
]);

module.exports.default = async ({ page }) => {
  const context = page.context();
  const guardKey = "__jiuwenBrowserNetworkGuardInstalled";
  if (context[guardKey]) return;
  context[guardKey] = true;
  await context.route("**/*", async (route) => {
    if (isAllowedRequestUrl(route.request().url())) {
      await route.continue();
      return;
    }
    await route.abort("blockedbyclient");
  });
};

function isAllowedRequestUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return false;
  }
  if (["about:", "blob:", "data:"].includes(parsed.protocol)) return true;
  if (parsed.protocol !== "https:") return false;

  const host = normalizeHost(parsed.hostname);
  if (!host || !host.includes(".")) return false;
  if (METADATA_HOSTS.has(host) || PUBLIC_SUFFIX_ONLY_HOSTS.has(host)) return false;
  if (INTERNAL_HOSTS.some((name) => host === name || host.endsWith(`.${name}`))) return false;
  if (isIpv4Host(host)) return isPublicIpv4(host);
  return !host.includes(":");
}

function normalizeHost(host) {
  return String(host || "")
    .trim()
    .replace(/^\[/, "")
    .replace(/\]$/, "")
    .replace(/\.$/, "")
    .toLowerCase();
}

function isIpv4Host(host) {
  return /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host);
}

function isPublicIpv4(host) {
  const parts = host.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [a, b] = parts;
  if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
  if (a === 100 && b >= 64 && b <= 127) return false;
  if (a === 169 && b === 254) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  return !(a === 192 && b === 168);
}

module.exports.__test = { isAllowedRequestUrl };
