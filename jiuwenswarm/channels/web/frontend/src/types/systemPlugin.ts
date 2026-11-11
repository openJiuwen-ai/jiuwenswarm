import type { LocalizedText } from './localized';

export type { LocalizedText } from './localized';

/** Where the card came from: bundled/installed locally or the SkillHub marketplace. */
export type SystemPluginSource = 'local' | 'hub';

export interface SystemPluginSummary {
  id: string;
  /** Installed package directory name; falls back to ``id`` for local presets. */
  packageId?: string;
  /** Hub asset id; empty for packages that were never downloaded from the Hub. */
  assetId?: string;
  version: string;
  /** Version currently installed on disk; empty when the package is not installed. */
  installedVersion?: string;
  displayName: LocalizedText;
  displayDescription: LocalizedText;
  tags: LocalizedText[];
  avatar: string | null;
  source?: SystemPluginSource;
  origin?: 'hub' | 'user' | 'preset';
  installed: boolean;
  active: boolean;
  enabled: boolean;
}

export interface SystemPluginEvent {
  name: string;
  description: LocalizedText;
}

export interface SystemPluginDetail extends SystemPluginSummary {
  /** Published versions from the Hub, oldest first; local packages expose only their own. */
  versions?: string[];
  details: LocalizedText;
  readme: LocalizedText;
  preview: string | null;
  events: SystemPluginEvent[];
}
