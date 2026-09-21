export type PublishAssetKind = 'skill' | 'agent_template' | 'agent_group' | 'plugin' | 'mcp';
export type AssetReference = { kind: PublishAssetKind; local_id: string };
export type AssetPublishOpenRequest = AssetReference & { avatar_url?: string };
export type PublishMetadata = {
  asset_name: string;
  display_name: string;
  version: string;
  description: string;
  tags: string[];
  version_desc: string;
  visibility: 'public' | 'private';
  dependency_sources?: Record<string, string>;
};
export type PublishIssue = string | { code?: string; message?: string; path?: string; [key: string]: unknown };
export type PublishRecord = {
  operation_id: string;
  draft_id?: string;
  version?: string;
  execution_status: 'queued' | 'uploading' | 'completed' | 'failed' | 'unknown';
  result: { publish_result: string; asset_id?: string; version?: string; [key: string]: unknown } | null;
  error: { code?: string; message?: string } | null;
  updated_at?: string | number;
};
export type PublishDescription = AssetReference & {
  defaults: PublishMetadata;
  can_publish: boolean;
  errors: PublishIssue[];
  records: PublishRecord[];
  identity_verified: boolean;
  hub_url?: string;
};
export type PublishDraft = {
  draft_id?: string;
  expires_at?: string | number;
  package_name: string;
  version: string;
  checksum_sha256?: string;
  artifact_sha256?: string;
  size_bytes: number;
  files: unknown[];
  excluded: unknown[];
  normalizations: unknown[];
  dependencies: unknown[];
  warnings: PublishIssue[];
  errors: PublishIssue[];
  can_submit: boolean;
};
