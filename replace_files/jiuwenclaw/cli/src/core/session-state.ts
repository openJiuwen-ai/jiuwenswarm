import crypto from "node:crypto";

export function generateSessionId(): string {
  const ts = Math.floor(Date.now() / 1000).toString(16);
  const suffix = crypto.randomBytes(3).toString("hex");
  return `sess_${ts}_${suffix}`;
}
