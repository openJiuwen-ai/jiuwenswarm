import crypto from "node:crypto";

export function generateCreateToken(): string {
  return crypto.randomUUID();
}
