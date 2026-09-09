export const mediaCapabilityModalities = ['vision', 'audio', 'video', 'image_gen', 'video_gen'] as const;

export type MediaCapabilityModality = (typeof mediaCapabilityModalities)[number];

export const mediaCapabilityConfigSuffixes = ['api_base', 'api_key', 'model', 'provider'] as const;

export const mediaCapabilityProviderMetadataSuffixes = ['endpoint_profile', 'vendor_key', 'plan'] as const;

export function mediaCapabilityConfigFields(modality: MediaCapabilityModality): string[] {
  return mediaCapabilityConfigSuffixes.map((suffix) => `${modality}_${suffix}`);
}

export function mediaCapabilityProviderMetadataFields(modality: MediaCapabilityModality): string[] {
  return mediaCapabilityProviderMetadataSuffixes.map((suffix) => `${modality}_${suffix}`);
}

export function mediaCapabilityPersistenceFields(modality: MediaCapabilityModality): string[] {
  return [...mediaCapabilityConfigFields(modality), ...mediaCapabilityProviderMetadataFields(modality)];
}

export function mediaCapabilityEnabledField(modality: MediaCapabilityModality): string {
  return `${modality}_enabled`;
}

export function isMediaCapabilityConfigured(
  values: Readonly<Record<string, unknown>>,
  modality: MediaCapabilityModality,
): boolean {
  return mediaCapabilityConfigFields(modality).every((field) => String(values[field] ?? '').trim());
}

export function wasConfigAppliedWithoutRestart(result: unknown): boolean {
  return (
    typeof result === 'object' &&
    result !== null &&
    'applied_without_restart' in result &&
    result.applied_without_restart === true
  );
}
