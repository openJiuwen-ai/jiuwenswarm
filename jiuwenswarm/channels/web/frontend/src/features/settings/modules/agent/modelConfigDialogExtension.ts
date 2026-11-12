import type { ReactNode } from 'react';

/** Optional catalogue metadata around an existing connection editor. */
export type ModelConfigDialogExtension = {
  render: (disabled: boolean) => ReactNode;
  renderDisplayName: (disabled: boolean) => ReactNode;
  dirty: boolean;
  fieldErrors?: Record<string, string>;
  onDraftChange: (updates: Record<string, string>) => void;
  /** Provider resets include the previous name; blur/select commits omit it. */
  onModelNameCommit: (modelName: string, previousModelName?: string) => void;
};

export class ModelConfigSaveCancelled extends Error {}
