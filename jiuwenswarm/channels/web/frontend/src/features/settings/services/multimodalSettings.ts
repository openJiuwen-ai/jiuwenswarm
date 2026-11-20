import type { SettingsRequest } from './settingsContract';
import type { SettingsSaveQueue } from './SettingsSaveQueue';

export const MULTIMODAL_CAPABILITIES = ['vision', 'audio', 'video', 'visual_gen', 'video_gen'] as const;
export type MultimodalCapability = (typeof MULTIMODAL_CAPABILITIES)[number];
export type MultimodalPurpose = 'understanding' | 'generation';
export type MultimodalConnection = {
  provider: string;
  protocol: string;
  api_base: string;
  api_key: string;
  model_name: string;
  endpoint_profile?: string;
  vendor_key?: string;
  plan?: string;
  context_window_tokens?: number | string;
};
export type MultimodalModel = {
  model_id: string;
  display_name: string;
  purpose: MultimodalPurpose;
  capabilities: MultimodalCapability[];
  connection: MultimodalConnection;
};
export type PublicMultimodalModel = Omit<MultimodalModel, 'connection'> & {
  provider: string;
  protocol: string;
  model_name: string;
  api_base: string;
  api_key_configured: boolean;
  references: MultimodalCapability[];
};
export type MultimodalSnapshot = {
  schema_version: 1;
  models: PublicMultimodalModel[];
  tools: Record<MultimodalCapability, { model_id: string | null; enabled: boolean }>;
};
export type MultimodalChange = {
  saved: true;
  changed: boolean;
  applied_without_restart: boolean;
  changed_tools: MultimodalCapability[];
  snapshot: MultimodalSnapshot;
};
export type MultimodalFieldError = { field: string; key: string; args?: Record<string, unknown> };
export type MultimodalErrorDetails = {
  code?: string;
  affected_tools?: MultimodalCapability[];
  activation_tools?: MultimodalCapability[];
  field_errors?: MultimodalFieldError[];
};
export function multimodalErrorDetails(error: unknown): MultimodalErrorDetails {
  const value = error as { code?: string; payload?: MultimodalErrorDetails } | null;
  return { ...value?.payload, code: value?.payload?.code ?? value?.code };
}

export function multimodalPurpose(tool: MultimodalCapability): MultimodalPurpose {
  return MULTIMODAL_CAPABILITIES.indexOf(tool) < 3 ? 'understanding' : 'generation';
}

/** List label for a catalogue row's provider.
 *
 * vLLM-Omni still saves ``provider`` as OpenAI (the vendor preset's
 * ``client_provider``, a value ``ProviderType`` will accept). The protocol is
 * what distinguishes it, so the multimodal model list must not show OpenAI.
 */
export function multimodalProviderLabel(model: Pick<PublicMultimodalModel, 'provider' | 'protocol'>): string {
  return String(model.protocol ?? '').trim().toLowerCase() === 'vllm-omni' ? 'vLLM-Omni' : model.provider;
}

export function capabilitiesFor(purpose: MultimodalPurpose): MultimodalCapability[] {
  return MULTIMODAL_CAPABILITIES.filter((tool) => multimodalPurpose(tool) === purpose);
}

export function multimodalModelOptions(models: PublicMultimodalModel[], tool: MultimodalCapability) {
  return models.filter((model) => model.capabilities.includes(tool)).map((model) => ({
    value: model.model_id,
    label: model.display_name,
    title: [model.model_name, model.api_base, model.model_id].filter(Boolean).join('\n'),
  }));
}

export type MultimodalSettingsState = {
  snapshot: MultimodalSnapshot | null;
  loading: boolean;
  saving: boolean;
  error: { source: 'read' | 'write'; cause: unknown } | null;
  lastChange: MultimodalChange | null;
  notice?: { tool: MultimodalCapability } | null;
};

/** Owns one shared snapshot for the model tab and the five tool controls. */
export class MultimodalSettingsStore {
  private state: MultimodalSettingsState = { snapshot: null, loading: false, saving: false, error: null, lastChange: null };
  private listeners = new Set<() => void>();
  private readSequence = 0;
  private loading: Promise<void> | null = null;

  constructor(private request: SettingsRequest, private saveQueue: SettingsSaveQueue) {}

  getSnapshot = (): MultimodalSettingsState => this.state;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private publish(patch: Partial<MultimodalSettingsState>): void {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((listener) => listener());
  }

  load = (force = false): Promise<void> => {
    if (this.loading && !force) return this.loading;
    const sequence = ++this.readSequence;
    this.publish({ loading: true, error: force || this.state.error?.source !== 'write' ? null : this.state.error });
    this.loading = this.request<MultimodalSnapshot>('multimodal.models.list', {}, { timeoutMs: 30_000 })
      .then((snapshot) => {
        if (sequence === this.readSequence) this.publish({ snapshot });
      })
      .catch((error: unknown) => {
        if (sequence === this.readSequence && this.state.error?.source !== 'write') {
          this.publish({ error: { source: 'read', cause: error } });
        }
      })
      .finally(() => {
        if (sequence === this.readSequence) {
          this.loading = null;
          this.publish({ loading: false });
        }
      });
    return this.loading;
  };

  getModel(modelId: string): Promise<{ model: MultimodalModel; references: MultimodalCapability[] }> {
    return this.request('multimodal.models.get', { model_id: modelId }, { timeoutMs: 30_000 });
  }

  announceShortcutChange(tool: MultimodalCapability): void {
    this.publish({ notice: { tool } });
  }

  async write(
    operation: 'models.upsert' | 'models.delete' | 'tools.update' | 'config.apply',
    params: Record<string, unknown>,
  ): Promise<MultimodalChange> {
    // Starting an already queued write must not clear a previous failure.
    this.publish({ error: null });
    return this.saveQueue.enqueue(`multimodal.${operation}`, async () => {
      this.publish({ saving: true, notice: null });
      try {
        const result = await this.request<MultimodalChange>(`multimodal.${operation}`, {
          ...params,
        }, { timeoutMs: 600_000 });
        // A list response issued before this write cannot replace its snapshot.
        this.readSequence += 1;
        this.loading = null;
        this.publish({ snapshot: result.snapshot, lastChange: result, loading: false });
        return result;
      } catch (error) {
        // The model editor owns validation feedback; other failures remain page-visible.
        if (operation !== 'models.upsert' || multimodalErrorDetails(error).code !== 'MM_VALIDATION_FAILED') {
          this.publish({ error: { source: 'write', cause: error } });
        }
        throw error;
      } finally {
        this.publish({ saving: false });
      }
    }, { errorScope: 'caller', successFeedback: false });
  }
}
