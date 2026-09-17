/**
 * Auto-Harness state management (multi-session version)
 *
 * Manages the frontend state for auto-harness execution,
 * including stage progress and harness messages.
 *
 * All harness runtime state is isolated per session in runtimes.
 * Package management state remains global.
 */

import { create } from 'zustand';
import { PackageInfo, NativeVersionInfo } from '../types';

/**
 * Stage status types
 */
export type HarnessStageStatus = 'running' | 'success' | 'failed' | 'timeout' | 'pending';
export type ExtensionProgressStatus = HarnessStageStatus | 'waiting' | 'skipped' | 'rejected';

/**
 * Stage definition received from backend pipeline message
 */
export interface HarnessStageDefinition {
  slot: string; // English key (assess, plan, etc.)
  display_name: string; // Chinese label (评估扩展缺口, 设计扩展方案, etc.)
}

/**
 * Information about a single stage execution
 */
export interface HarnessStageInfo {
  stage: string; // English key/slot (assess, plan, etc.)
  stageLabel?: string; // Chinese label from harness.message content or stage definition
  status: HarnessStageStatus;
  error?: string;
  messages: string[];
  metrics: Record<string, unknown>;
}

/**
 * Harness message entry
 */
export interface HarnessMessageEntry {
  content: string;
  timestamp: number;
  stage?: string;
}

/**
 * Extension ready information received from backend
 */
export interface ExtensionReadyInfo {
  extensionName: string;
  runtimePath: string;
  sessionRuntimePath?: string;
  extensionRuntimePath?: string;
  configPath: string;
  runtimeExtensions?: RuntimeExtensionInfo[];
  verifyReport: Record<string, unknown>;
  componentsSummary: Record<string, unknown>;
}

export interface RuntimeExtensionInfo {
  extensionName: string;
  runtimePath: string;
  configPath: string;
}

export interface ExtensionProgressInfo {
  extensionName: string;
  taskId?: string;
  implementStatus: ExtensionProgressStatus;
  verifyStatus: ExtensionProgressStatus;
  activateStatus: ExtensionProgressStatus;
  error?: string;
  messages: string[];
}

/**
 * Activate interaction state for user confirmation
 */
export interface ActivateInteractionState {
  interactionId: string;
  extensionName: string;
  runtimePath: string;
  options: string[];
  pending: boolean;
}

/**
 * File tree entry for caching
 */
export interface CachedFileTreeEntry {
  name: string;
  path: string;
  is_dir: boolean;
  children?: CachedFileTreeEntry[];
}

/**
 * Single session harness runtime state.
 * All session-level fields are isolated here.
 */
interface HarnessRuntime {
  // Stage definitions from backend pipeline
  stageDefinitions: HarnessStageDefinition[];
  // Stage progress messages list
  harnessMessages: HarnessMessageEntry[];
  // Stage execution results
  stageResults: HarnessStageInfo[];
  // Current active stage name (English key)
  currentStage: string | null;
  // Whether harness is running
  isHarnessRunning: boolean;
  // Overall progress percentage (0-100)
  progressPercent: number;
  // Extension ready information for file tree display
  extensionReady: ExtensionReadyInfo | null;
  sessionRuntimePath: string;
  runtimeExtensions: RuntimeExtensionInfo[];
  extensionOrder: string[];
  extensionsByName: Record<string, ExtensionProgressInfo>;
  // Activate interaction pending state
  activateInteraction: ActivateInteractionState | null;
}

/**
 * Harness state interface
 */
interface HarnessState {
  runtimes: Record<string, HarnessRuntime>;

  /** App-wide notification, deliberately separate from session-bound harness state. */
  proactiveNotificationMessage: string | null;

  // Package list from backend (global)
  packages: PackageInfo[];
  // Native version info
  nativeVersion: NativeVersionInfo | null;
  // Currently active package IDs (multiple can be active simultaneously)
  activePackageIds: string[];
  // Selected package ID in dropdown (not yet activated)
  selectedPackageId: string | null;
  // Loading state for packages
  loadingPackages: boolean;
  // Activating state
  activatingPackage: boolean;
  // Deactivating state
  deactivatingPackage: boolean;

  // File tree cache by runtime path (key: runtimePath, value: file tree)
  extensionFileTreeCache: Record<string, CachedFileTreeEntry[]>;
  // Loading state for file tree by path
  fileTreeLoadingPaths: Record<string, boolean>;

  // Runtime management
  ensureRuntime: (sessionId: string) => HarnessRuntime;
  getRuntime: (sessionId: string | null) => HarnessRuntime | undefined;
  removeRuntime: (sessionId: string) => void;

  // Session-level actions
  setStageDefinitions: (sessionId: string, stages: HarnessStageDefinition[]) => void;
  addHarnessMessage: (sessionId: string, content: string, stage?: string) => void;
  updateStageResult: (sessionId: string, info: HarnessStageInfo) => void;
  setCurrentStage: (sessionId: string, stage: string | null) => void;
  setHarnessRunning: (sessionId: string, running: boolean) => void;
  setExtensionReady: (sessionId: string, info: ExtensionReadyInfo | null) => void;
  updateExtensionProgress: (sessionId: string, info: {
    extensionName: string;
    taskId?: string;
    parentStage?: string;
    extensionStage?: string;
    status: ExtensionProgressStatus;
    error?: string;
    messages?: string[];
  }) => void;
  setActivateInteraction: (sessionId: string, state: ActivateInteractionState | null) => void;
  reset: (sessionId: string) => void;
  setProactiveNotification: (message: string | null) => void;

  // Global package actions
  setPackages: (packages: PackageInfo[], nativeVersion: NativeVersionInfo, activeIds: string[]) => void;
  isPackageActive: (packageId: string) => boolean;
  setSelectedPackageId: (id: string | null) => void;
  setLoadingPackages: (loading: boolean) => void;
  setActivatingPackage: (activating: boolean) => void;
  setDeactivatingPackage: (deactivating: boolean) => void;

  // File tree cache actions (global)
  setFileTreeCache: (runtimePath: string, files: CachedFileTreeEntry[]) => void;
  getFileTreeCache: (runtimePath: string) => CachedFileTreeEntry[] | undefined;
  clearFileTreeCache: (runtimePath?: string) => void;
  setFileTreeLoading: (runtimePath: string, loading: boolean) => void;
  isFileTreeLoading: (runtimePath: string) => boolean;
}

/**
 * Create initial pending stages from stage definitions
 */
function createInitialStages(stageDefinitions: HarnessStageDefinition[]): HarnessStageInfo[] {
  return stageDefinitions.map((stage) => ({
    stage: stage.slot,
    stageLabel: stage.display_name,
    status: 'pending' as HarnessStageStatus,
    messages: [],
    metrics: {},
  }));
}

/**
 * Calculate progress percentage based on completed stages
 */
function calculateProgressPercent(stageResults: HarnessStageInfo[]): number {
  const totalStages = stageResults.length;
  if (totalStages === 0) return 0;
  const completedCount = stageResults.filter(
    s => s.status === 'success' || s.status === 'failed' || s.status === 'timeout'
  ).length;
  const runningStage = stageResults.find(s => s.status === 'running');
  // Add 0.5 for running stage to show partial progress
  const runningProgress = runningStage ? 0.5 : 0;
  return Math.round(((completedCount + runningProgress) / totalStages) * 100);
}

function extractDesignNames(messages: string[]): string[] {
  const names: string[] = [];
  for (const message of messages) {
    const normalized = message.trim();
    if (!normalized.startsWith('Designs:')) continue;
    const raw = normalized.slice('Designs:'.length).trim();
    for (const part of raw.split(',')) {
      const name = part.trim();
      if (name && !names.includes(name)) {
        names.push(name);
      }
    }
  }
  return names;
}

function stageIndex(stageResults: HarnessStageInfo[], stage: string): number {
  return stageResults.findIndex((item) => item.stage === stage);
}

function hasLaterActiveStage(stageResults: HarnessStageInfo[], stage: string): boolean {
  const index = stageIndex(stageResults, stage);
  if (index < 0) return false;
  return stageResults.slice(index + 1).some(
    (item) => item.status === 'running' || item.status === 'success' || item.status === 'failed' || item.status === 'timeout'
  );
}

function createEmptyRuntime(): HarnessRuntime {
  return {
    stageDefinitions: [],
    harnessMessages: [],
    stageResults: [],
    currentStage: null,
    isHarnessRunning: false,
    progressPercent: 0,
    extensionReady: null,
    sessionRuntimePath: '',
    runtimeExtensions: [],
    extensionOrder: [],
    extensionsByName: {},
    activateInteraction: null,
  };
}

export const useHarnessStore = create<HarnessState>((set, get) => ({
  runtimes: {},
  proactiveNotificationMessage: null,

  packages: [],
  nativeVersion: null,
  activePackageIds: [],
  selectedPackageId: null,
  loadingPackages: false,
  activatingPackage: false,
  deactivatingPackage: false,

  extensionFileTreeCache: {},
  fileTreeLoadingPaths: {},

  ensureRuntime: (sessionId) => {
    const existing = get().runtimes[sessionId];
    if (existing) return existing;
    const runtime = createEmptyRuntime();
    set((state) => ({
      runtimes: { ...state.runtimes, [sessionId]: runtime },
    }));
    return runtime;
  },

  getRuntime: (sessionId) => {
    if (!sessionId) return undefined;
    return get().runtimes[sessionId];
  },

  removeRuntime: (sessionId) => {
    set((state) => {
      const next = { ...state.runtimes };
      delete next[sessionId];
      return { runtimes: next };
    });
  },

  setStageDefinitions: (sessionId, stages) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      // Only set stages once - ignore subsequent updates
      if (runtime.stageDefinitions.length > 0) {
        // Already initialized, don't update
        return {};
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            stageDefinitions: stages,
            stageResults: createInitialStages(stages),
          },
        },
      };
    });
  },

  addHarnessMessage: (sessionId, content, stage) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            harnessMessages: [
              ...runtime.harnessMessages,
              {
                content,
                timestamp: Date.now(),
                stage,
              },
            ],
          },
        },
      };
    });
  },

  updateStageResult: (sessionId, info) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const shouldIgnoreRollback = info.status === 'running' && hasLaterActiveStage(runtime.stageResults, info.stage);
      if (shouldIgnoreRollback) {
        return {};
      }
      const existingIndex = runtime.stageResults.findIndex((s) => s.stage === info.stage);
      const existingLabel = existingIndex >= 0 ? runtime.stageResults[existingIndex].stageLabel : undefined;
      const definitionLabel = runtime.stageDefinitions.find(d => d.slot === info.stage)?.display_name;
      const stageLabel = info.stageLabel || existingLabel || definitionLabel || info.stage;

      let newStageResults: HarnessStageInfo[];
      if (existingIndex >= 0) {
        newStageResults = [...runtime.stageResults];
        newStageResults[existingIndex] = {
          ...newStageResults[existingIndex],
          stageLabel,
          status: info.status,
          error: info.error,
          messages: info.messages,
          metrics: info.metrics,
        };
      } else {
        newStageResults = [...runtime.stageResults, { ...info, stageLabel }];
      }

      const nextExtensionOrder = [...runtime.extensionOrder];
      const nextExtensionsByName = { ...runtime.extensionsByName };
      if (info.stage === 'plan' && info.messages?.length) {
        for (const extensionName of extractDesignNames(info.messages)) {
          if (!nextExtensionOrder.includes(extensionName)) {
            nextExtensionOrder.push(extensionName);
          }
          nextExtensionsByName[extensionName] = {
            ...nextExtensionsByName[extensionName],
            extensionName,
            implementStatus: nextExtensionsByName[extensionName]?.implementStatus || 'pending',
            verifyStatus: nextExtensionsByName[extensionName]?.verifyStatus || 'pending',
            activateStatus: nextExtensionsByName[extensionName]?.activateStatus || 'pending',
            messages: nextExtensionsByName[extensionName]?.messages || [],
          };
        }
      }

      if (info.status === 'running' || info.status === 'success') {
        const activeIndex = newStageResults.findIndex((s) => s.stage === info.stage);
        if (activeIndex > 0) {
          newStageResults = newStageResults.map((stageInfo, index) => {
            if (index >= activeIndex || stageInfo.status === 'success') return stageInfo;
            if (stageInfo.status === 'pending' || stageInfo.status === 'running') {
              return { ...stageInfo, status: 'success' as HarnessStageStatus };
            }
            return stageInfo;
          });
        }
      }

      let newCurrentStage = runtime.currentStage;
      if (info.status === 'running') newCurrentStage = info.stage;
      else if (runtime.currentStage === info.stage && (info.status === 'success' || info.status === 'failed' || info.status === 'timeout')) {
        newCurrentStage = null;
      }

      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            stageResults: newStageResults,
            currentStage: newCurrentStage,
            progressPercent: calculateProgressPercent(newStageResults),
            extensionOrder: nextExtensionOrder,
            extensionsByName: nextExtensionsByName,
          },
        },
      };
    });
  },

  setCurrentStage: (sessionId, stage) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, currentStage: stage },
        },
      };
    });
  },

  setHarnessRunning: (sessionId, running) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: { ...runtime, isHarnessRunning: running },
        },
      };
    });
  },

  setExtensionReady: (sessionId, info) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (!info) {
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: {
              ...runtime,
              extensionReady: null,
              sessionRuntimePath: '',
              runtimeExtensions: [],
            },
          },
        };
      }
      const runtimeExtensions = info.runtimeExtensions || runtime.runtimeExtensions;
      const extensionOrder = [...runtime.extensionOrder];
      const extensionsByName = { ...runtime.extensionsByName };
      for (const ext of runtimeExtensions) {
        if (!extensionOrder.includes(ext.extensionName)) {
          extensionOrder.push(ext.extensionName);
        }
        extensionsByName[ext.extensionName] = {
          ...extensionsByName[ext.extensionName],
          extensionName: ext.extensionName,
          implementStatus: 'success',
          verifyStatus: 'success',
          activateStatus: extensionsByName[ext.extensionName]?.activateStatus || 'pending',
          messages: extensionsByName[ext.extensionName]?.messages || [],
        };
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            extensionReady: info,
            sessionRuntimePath: info.sessionRuntimePath || info.runtimePath,
            runtimeExtensions,
            extensionOrder,
            extensionsByName,
          },
        },
      };
    });
  },

  updateExtensionProgress: (sessionId, info) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      const existing = runtime.extensionsByName[info.extensionName] || {
        extensionName: info.extensionName,
        implementStatus: 'pending' as ExtensionProgressStatus,
        verifyStatus: 'pending' as ExtensionProgressStatus,
        activateStatus: 'pending' as ExtensionProgressStatus,
        messages: [],
      };
      const next: ExtensionProgressInfo = {
        ...existing,
        taskId: info.taskId || existing.taskId,
        error: info.error || existing.error,
        messages: [...existing.messages, ...(info.messages || [])],
      };
      if (info.parentStage === 'activate' || info.extensionStage === 'activate_ext') {
        next.activateStatus = info.status;
      } else if (info.extensionStage === 'implement_ext') {
        next.implementStatus = info.status;
      } else if (info.extensionStage === 'verify_ext') {
        next.verifyStatus = info.status;
      }
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            extensionOrder: runtime.extensionOrder.includes(info.extensionName)
              ? runtime.extensionOrder
              : [...runtime.extensionOrder, info.extensionName],
            extensionsByName: {
              ...runtime.extensionsByName,
              [info.extensionName]: next,
            },
          },
        },
      };
    });
  },

  setActivateInteraction: (sessionId, interaction) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      if (!interaction) {
        return {
          runtimes: {
            ...state.runtimes,
            [sessionId]: { ...runtime, activateInteraction: null },
          },
        };
      }
      const existing = runtime.extensionsByName[interaction.extensionName] || {
        extensionName: interaction.extensionName,
        implementStatus: 'success' as ExtensionProgressStatus,
        verifyStatus: 'success' as ExtensionProgressStatus,
        activateStatus: 'pending' as ExtensionProgressStatus,
        messages: [],
      };
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: {
            ...runtime,
            activateInteraction: interaction,
            extensionOrder: runtime.extensionOrder.includes(interaction.extensionName)
              ? runtime.extensionOrder
              : [...runtime.extensionOrder, interaction.extensionName],
            extensionsByName: {
              ...runtime.extensionsByName,
              [interaction.extensionName]: {
                ...existing,
                activateStatus: interaction.pending ? 'running' : existing.activateStatus,
              },
            },
          },
        },
      };
    });
  },

  reset: (sessionId) => {
    set((state) => {
      const runtime = state.runtimes[sessionId];
      if (!runtime) return state;
      return {
        runtimes: {
          ...state.runtimes,
          [sessionId]: createEmptyRuntime(),
        },
      };
    });
  },

  setProactiveNotification: (message) => {
    set({ proactiveNotificationMessage: message });
  },

  setPackages: (packages, nativeVersion, activeIds) => {
    const currentSelection = get().selectedPackageId;
    const allIds = ['native', ...packages.map(p => p.id)];
    const newSelection = (currentSelection && allIds.includes(currentSelection)) ? currentSelection : 'native';
    set({
      packages,
      nativeVersion,
      activePackageIds: activeIds,
      selectedPackageId: newSelection,
    });
  },

  isPackageActive: (packageId) => {
    const state = get();
    return state.activePackageIds.includes(packageId);
  },

  setSelectedPackageId: (id) => {
    set({ selectedPackageId: id });
  },

  setLoadingPackages: (loading) => {
    set({ loadingPackages: loading });
  },

  setActivatingPackage: (activating) => {
    set({ activatingPackage: activating });
  },

  setDeactivatingPackage: (deactivating) => {
    set({ deactivatingPackage: deactivating });
  },

  // File tree cache actions
  setFileTreeCache: (runtimePath, files) => {
    set((state) => ({
      extensionFileTreeCache: {
        ...state.extensionFileTreeCache,
        [runtimePath]: files,
      },
      fileTreeLoadingPaths: {
        ...state.fileTreeLoadingPaths,
        [runtimePath]: false,
      },
    }));
  },

  getFileTreeCache: (runtimePath) => {
    return get().extensionFileTreeCache[runtimePath];
  },

  clearFileTreeCache: (runtimePath) => {
    if (runtimePath) {
      set((state) => {
        const newCache = { ...state.extensionFileTreeCache };
        delete newCache[runtimePath];
        const newLoading = { ...state.fileTreeLoadingPaths };
        delete newLoading[runtimePath];
        return {
          extensionFileTreeCache: newCache,
          fileTreeLoadingPaths: newLoading,
        };
      });
    } else {
      set({
        extensionFileTreeCache: {},
        fileTreeLoadingPaths: {},
      });
    }
  },

  setFileTreeLoading: (runtimePath, loading) => {
    set((state) => ({
      fileTreeLoadingPaths: {
        ...state.fileTreeLoadingPaths,
        [runtimePath]: loading,
      },
    }));
  },

  isFileTreeLoading: (runtimePath) => {
    return get().fileTreeLoadingPaths[runtimePath] || false;
  },
}));
