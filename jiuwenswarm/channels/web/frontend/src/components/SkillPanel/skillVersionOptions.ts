export type SkillVersionOptionSource = {
  version: string;
  is_default: boolean;
  available: boolean;
};

export type SkillVersionOption = {
  version: string;
  disabled: boolean;
  label: string;
};

export type SkillVersionOptionLabels = {
  defaultSuffix: string;
  unavailableSuffix: string;
};

export function buildSkillVersionOptions(
  versions: readonly SkillVersionOptionSource[],
  labels: SkillVersionOptionLabels,
): SkillVersionOption[] {
  return versions
    .filter((entry) => typeof entry.version === 'string' && entry.version.trim().length > 0)
    .map((entry) => {
      const disabled = !entry.available;
      const defaultSuffix = entry.is_default ? labels.defaultSuffix : '';
      const unavailableSuffix = disabled ? labels.unavailableSuffix : '';
      return {
        version: entry.version,
        disabled,
        label: `${entry.version}${defaultSuffix}${unavailableSuffix}`,
      };
    });
}
