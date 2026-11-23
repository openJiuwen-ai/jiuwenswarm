import type { LocalizedText } from './localized';

export type { LocalizedText } from './localized';

/** Where the card came from: bundled/installed locally or the SkillHub marketplace. */
export type SystemPluginSource = 'local' | 'hub';

/** A standalone system plugin or a bundle that carries several members. */
export type SystemPluginKind = 'plugin' | 'bundle';

/** Aggregate state of a bundle, or the single-plugin state in the same vocabulary. */
export type SystemPluginOverallStatus = 'not-installed' | 'disabled' | 'partial' | 'enabled' | 'failed';

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
  kind?: SystemPluginKind;
  membersCount?: number;
  overallStatus?: SystemPluginOverallStatus;
  installed: boolean;
  active: boolean;
  enabled: boolean;
}

export interface SystemPluginEvent {
  name: string;
  description: LocalizedText;
}

/** One member of a bundle, shown inside the bundle detail page. */
export interface SystemPluginMember {
  packageId: string;
  displayName: LocalizedText;
  displayDescription: LocalizedText;
  version: string;
  domain: string;
  status?: 'pending' | 'active' | 'standby' | 'disabled' | 'failed';
  active: boolean;
  enabled: boolean;
  error?: string;
  installedVersion: string;
}

export interface SystemPluginDetail extends SystemPluginSummary {
  /** Published versions from the Hub, oldest first; local packages expose only their own. */
  versions?: string[];
  details: LocalizedText;
  readme: LocalizedText;
  preview: string | null;
  /** Only standalone plugins declare events; a bundle uses ``systemPlugins`` instead. */
  events: SystemPluginEvent[];
  systemPlugins?: SystemPluginMember[];
}
