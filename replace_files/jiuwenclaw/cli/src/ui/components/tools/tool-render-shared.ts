import { summarize } from "../../rendering/text.js";
import { isPlainObject, tryParseStructuredText } from "./tool-structured-data.js";

export * from "./tool-kind-utils.js";
export * from "./tool-line-renderers.js";
export * from "./tool-structured-data.js";

function summarizeStructuredPayload(value: string): string | undefined {
  const parsed = tryParseStructuredText(value);
  if (Array.isArray(parsed)) return `${parsed.length} item${parsed.length === 1 ? "" : "s"}`;
  if (isPlainObject(parsed)) {
    const keys = Object.keys(parsed);
    return `${keys.length} field${keys.length === 1 ? "" : "s"}`;
  }
  return undefined;
}

export function summarizeToolResultByKind(name: string, result: string): string | undefined {
  const normalized = name.toLowerCase();
  const lines = result.split("\n").filter(Boolean).length;
  if (normalized.includes("read") || normalized.includes("view")) return `${lines} lines loaded`;
  if (normalized.includes("search") || normalized.includes("grep")) return `${lines} matches`;
  if (normalized.includes("fetch") || normalized.includes("webpage"))
    return `${lines} lines fetched`;
  if (normalized.includes("edit") || normalized.includes("write") || normalized.includes("patch")) {
    return "edit applied";
  }
  if (
    normalized.includes("exec") ||
    normalized.includes("bash") ||
    normalized.includes("shell") ||
    normalized.includes("command")
  ) {
    return summarize(result.split("\n")[0] ?? "", 88);
  }
  return summarizeStructuredPayload(result);
}
