export function resolveNewConversationProjectDir(
  preserveProject: boolean | undefined,
  explicitProjectDir: string | undefined,
  selectedProjectDir: string | undefined,
): string | null {
  if (!preserveProject) return null;
  return explicitProjectDir ?? selectedProjectDir ?? null;
}
