import { useEffect } from 'react';
import { create } from 'zustand';
import { webRequest } from '../../services/webClient';
import { SETTINGS_CONFIG_CHANGED_EVENT } from '../settings/services/settingsContract';
import { multimodalErrorDetails, type MultimodalSnapshot } from '../settings/services/multimodalSettings';
import {
  readGenerationSelection,
  type DesignGenerationSelection,
  type DesignGenerationSlot,
} from './designerModelConfiguration';

export const DESIGN_GENERATION_CHANGED_EVENT = 'jiuwen:design-generation-changed';

const EMPTY_SELECTION: DesignGenerationSelection = { video_gen: null, visual_gen: null };

const DESIGN_GENERATION_CONFIG_KEY = {
  video_gen: 'design_generation_video_gen',
  visual_gen: 'design_generation_visual_gen',
} as const;

function selectionFromConfig(value: unknown): DesignGenerationSelection {
  const record = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const saved = (slot: DesignGenerationSlot): string | null => {
    const item = record[DESIGN_GENERATION_CONFIG_KEY[slot]];
    return typeof item === 'string' ? item : null;
  };
  return { video_gen: saved('video_gen'), visual_gen: saved('visual_gen') };
}

const EMPTY_SNAPSHOT: MultimodalSnapshot = {
  schema_version: 1,
  models: [],
  tools: {
    vision: { model_id: null, enabled: false },
    audio: { model_id: null, enabled: false },
    video: { model_id: null, enabled: false },
    visual_gen: { model_id: null, enabled: false },
    video_gen: { model_id: null, enabled: false },
  },
};

function catalogueIsMissing(error: unknown): boolean {
  return multimodalErrorDetails(error).code === 'MM_NOT_CONFIGURED';
}

type DesignerGenerationState = {
  snapshot: MultimodalSnapshot | null;
  selection: DesignGenerationSelection;
  loading: boolean;
  failed: boolean;
  selectError: string;
  reload: () => Promise<void>;
  select: (slot: DesignGenerationSlot, modelId: string) => Promise<void>;
};

let reloadEpoch = 0;
let selectionEpoch = 0;
let subscribers = 0;
let stopListening: (() => void) | null = null;

function publishChange(): void {
  window.dispatchEvent(new window.Event(DESIGN_GENERATION_CHANGED_EVENT));
}

export const useDesignerGenerationStore = create<DesignerGenerationState>((set, get) => ({
  snapshot: null,
  selection: EMPTY_SELECTION,
  loading: true,
  failed: false,
  selectError: '',
  reload: async () => {
    const epoch = ++reloadEpoch;
    const selectionAtStart = selectionEpoch;
    set({ loading: true, failed: false });
    try {
      const [snapshot, config] = await Promise.all([
        webRequest<MultimodalSnapshot>('multimodal.models.list', {}, { timeoutMs: 30_000 }),
        webRequest<Record<string, unknown>>('config.get', {}, { timeoutMs: 30_000 }),
      ]);
      if (epoch !== reloadEpoch) return;
      const models = Array.isArray(snapshot?.models) ? snapshot.models : [];
      const next: Partial<DesignerGenerationState> = {
        snapshot: snapshot ? { ...snapshot, models } : null,
        loading: false,
        failed: false,
      };
      if (selectionAtStart === selectionEpoch) {
        next.selection = readGenerationSelection(selectionFromConfig(config), models);
      }
      set(next);
      publishChange();
    } catch (error) {
      if (epoch !== reloadEpoch) return;
      if (catalogueIsMissing(error)) {
        const next: Partial<DesignerGenerationState> = {
          snapshot: EMPTY_SNAPSHOT,
          loading: false,
          failed: false,
        };
        if (selectionAtStart === selectionEpoch) next.selection = EMPTY_SELECTION;
        set(next);
        publishChange();
        return;
      }
      set({ failed: true, loading: false });
    }
  },
  select: async (slot, modelId) => {
    if (get().selection[slot] === modelId) return;
    const epoch = ++selectionEpoch;
    set({ selectError: '' });
    try {
      await webRequest(
        'config.set',
        { [DESIGN_GENERATION_CONFIG_KEY[slot]]: modelId },
        { timeoutMs: 30_000 },
      );
      if (epoch !== selectionEpoch) return;
      const models = get().snapshot?.models ?? [];
      set({
        selection: readGenerationSelection({ ...get().selection, [slot]: modelId }, models),
        selectError: '',
      });
      publishChange();
    } catch (error) {
      if (epoch !== selectionEpoch) return;
      set({ selectError: error instanceof Error ? error.message : String(error) });
    }
  },
}));

/** Keep one shared catalogue for the landing menu, the canvas menu and node menus. */
export function useDesignerModelConfiguration() {
  useEffect(() => {
    subscribers += 1;
    if (subscribers === 1) {
      const refresh = () => { void useDesignerGenerationStore.getState().reload(); };
      void refresh();
      window.addEventListener(SETTINGS_CONFIG_CHANGED_EVENT, refresh);
      window.addEventListener('focus', refresh);
      stopListening = () => {
        reloadEpoch += 1;
        window.removeEventListener(SETTINGS_CONFIG_CHANGED_EVENT, refresh);
        window.removeEventListener('focus', refresh);
      };
    }
    return () => {
      subscribers -= 1;
      if (subscribers === 0) {
        stopListening?.();
        stopListening = null;
      }
    };
  }, []);
  return useDesignerGenerationStore();
}
