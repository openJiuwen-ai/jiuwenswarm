export type ApplicationPluginNavKey = `app:${string}`;

export interface ApplicationPluginContribution {
  plugin_id: string;
  plugin_version: string;
  description?: string;
  permissions?: string[];
  enabled?: boolean;
  id: string;
  nav_key: string;
  title: string;
  title_i18n_key?: string;
  render_mode: 'bundled' | 'iframe' | 'none';
  component?: string;
  entry_url?: string;
  position: number;
}

export interface ApplicationPluginManifest {
  api_version: number;
  plugins: ApplicationPluginContribution[];
}

export interface ApplicationPluginSettingsProps {
  contribution: ApplicationPluginContribution;
  onManifestChanged: () => void;
}
