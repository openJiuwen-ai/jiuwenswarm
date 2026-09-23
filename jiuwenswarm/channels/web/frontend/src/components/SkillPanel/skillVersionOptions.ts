export type SkillVersionOptionSource = {
  version: string;
  is_default: boolean;
  available: boolean;
};

export type SkillVersionOption = {
  version: string;
  isDefault: boolean;
  disabled: boolean;
  label: string;
};

export function buildSkillVersionOptions(versions: readonly SkillVersionOptionSource[]): SkillVersionOption[] {
  return versions
    .filter((entry) => typeof entry.version === 'string' && entry.version.trim().length > 0)
    .map((entry) => ({
      version: entry.version,
      isDefault: entry.is_default,
      disabled: !entry.available,
      label: entry.version,
    }));
}
