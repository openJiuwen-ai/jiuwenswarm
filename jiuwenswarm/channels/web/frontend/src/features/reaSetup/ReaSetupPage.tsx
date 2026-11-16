import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { LoadingSpinner } from '../../components/ui';
import stepInstallArt from '../../assets/rea/step-install.png';
import stepEnhanceArt from '../../assets/rea/step-enhance.png';
import stepCallArt from '../../assets/rea/step-call.png';
import guideTargetArt from '../../assets/rea/guide-target.svg';
import guideMcpArt from '../../assets/rea/guide-mcp.svg';
import guideConfirmArt from '../../assets/rea/guide-confirm.svg';
import detectArt from '../../assets/rea/detect.png';
import SkillIcon from '../../assets/agent-management/agent-skill.svg?react';
import { reaSetupApi } from './reaSetupApi';
import {
  estimatePaceRemainingSeconds,
  estimateRemainingSeconds,
  formatProgressDuration,
  messageWithoutEta,
  persistActiveJob,
  readPersistedActiveJob,
  selectDisplayedRemainingSeconds,
  shouldDeferEnhancedDetection,
  stabilizeRemainingSeconds,
} from './reaSetupProgress';
import type {
  ReaAssistedInput,
  ReaSetupEnhancedItem,
  ReaSetupInstallResult,
  ReaSetupJob,
  ReaSetupPlanItem,
  ReaSetupScan,
  ReaSetupStep,
} from './types';
import './reaSetup.css';

const SCAN_CACHE_KEY = 'reaSetup.scan.v4';

type VisualState =
  | 'available'
  | 'installing'
  | 'needsAction'
  | 'detected'
  | 'unsupported'
  | 'notInstalled'
  | 'downloaded'
  | 'partial'
  | 'failed';

type GroupKind = 'base' | 'enhanced';

interface ReaSetupPageProps {
  onUseRea: (prompt: string) => void;
}

interface GroupStats {
  total: number;
  available: number;
  installed: number;
  installing: number;
  notInstalled: number;
  failed: number;
}

interface CapabilityCardView {
  id: string;
  label: string;
  description: string;
  usage: string;
  stateHint: string;
  hoverText: string;
  visualState: VisualState;
  actionItem?: ReaSetupEnhancedItem;
  members: CapabilityMemberView[];
}

interface CapabilityMemberView {
  id: string;
  label: string;
  visualState: VisualState;
  reason: string;
}

type PlanWithEnhanced = ReaSetupPlanItem & { enhanced?: ReaSetupEnhancedItem };

function readCachedScan(): ReaSetupScan | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(SCAN_CACHE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<ReaSetupScan>;
    if (
      !Array.isArray(value.base)
      || !Array.isArray(value.enhanced)
      || !Array.isArray(value.enhanced_plan)
      || !value.platform_id
    ) {
      return null;
    }
    return value as ReaSetupScan;
  } catch {
    return null;
  }
}

function writeCachedScan(scan: ReaSetupScan): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(SCAN_CACHE_KEY, JSON.stringify(scan));
  } catch {
    // Storage can be unavailable in private or restricted browser contexts.
  }
}

const CAPABILITY_SPECS = [
  { id: 'ghidra', labelKey: 'reaSetup.capability.ghidra', descriptionKey: 'reaSetup.capability.ghidraDesc', hoverKey: 'reaSetup.capabilityHover.ghidra', memberIds: ['java', 'ghidra'] },
  { id: 'ida', labelKey: 'reaSetup.capability.ida', descriptionKey: 'reaSetup.capability.idaDesc', hoverKey: 'reaSetup.capabilityHover.ida', memberIds: ['ida'] },
  { id: 'kuna', labelKey: 'reaSetup.capability.kuna', descriptionKey: 'reaSetup.capability.kunaDesc', hoverKey: 'reaSetup.capabilityHover.kuna', memberIds: ['kuna'] },
  { id: 'hopper', labelKey: 'reaSetup.capability.hopper', descriptionKey: 'reaSetup.capability.hopperDesc', hoverKey: 'reaSetup.capabilityHover.hopper', memberIds: ['hopper'] },
  { id: 'android', labelKey: 'reaSetup.capability.android', descriptionKey: 'reaSetup.capability.androidDesc', hoverKey: 'reaSetup.capabilityHover.android', memberIds: ['java', 'jadx', 'adb', 'apktool'] },
  { id: 'jeb', labelKey: 'reaSetup.capability.jeb', descriptionKey: 'reaSetup.capability.jebDesc', hoverKey: 'reaSetup.capabilityHover.jeb', memberIds: ['jeb'] },
  { id: 'apple', labelKey: 'reaSetup.capability.apple', descriptionKey: 'reaSetup.capability.appleDesc', hoverKey: 'reaSetup.capabilityHover.apple', memberIds: [] },
  { id: 'dynamic', labelKey: 'reaSetup.capability.dynamic', descriptionKey: 'reaSetup.capability.dynamicDesc', hoverKey: 'reaSetup.capabilityHover.dynamic', memberIds: ['gdb', 'rizin', 'cutter', 'chrome', 'frida', 'browser_electron', 'process_capture'] },
  { id: 'network_firmware', labelKey: 'reaSetup.capability.networkFirmware', descriptionKey: 'reaSetup.capability.networkFirmwareDesc', hoverKey: 'reaSetup.capabilityHover.networkFirmware', memberIds: ['pcap', 'mitmproxy', 'firmware', 'sigrok'] },
  { id: 'script_formats', labelKey: 'reaSetup.capability.scriptFormats', descriptionKey: 'reaSetup.capability.scriptFormatsDesc', hoverKey: 'reaSetup.capabilityHover.scriptFormats', memberIds: ['pwntools', 'wabt', 'wakaru', 'objdump'] },
] as const;

const MEMBER_FALLBACK_LABELS: Record<string, string> = {
  java: 'Java 21',
  ghidra: 'Ghidra',
  ida: 'IDA',
  kuna: 'Kuna',
  hopper: 'Hopper',
  jadx: 'JADX',
  adb: 'ADB',
  apktool: 'Apktool',
  jeb: 'JEB',
  apple_native: 'macOS native utilities',
  gdb: 'GDB',
  rizin: 'Rizin',
  cutter: 'Cutter',
  chrome: '浏览器运行时',
  frida: 'Frida',
  browser_electron: '浏览器 / Electron',
  process_capture: '进程捕获',
  pcap: 'PCAP / TShark',
  mitmproxy: 'mitmproxy',
  firmware: '固件提取',
  sigrok: 'sigrok',
  pwntools: 'pwntools',
  wabt: 'WABT',
  wakaru: 'Wakaru',
  objdump: 'objdump',
};

function mergePlan(
  plan: ReaSetupPlanItem[],
  enhanced: ReaSetupEnhancedItem[],
): PlanWithEnhanced[] {
  const enhancedById = new Map(enhanced.map((item) => [item.id, item]));
  return plan.map((item) => {
    const live = enhancedById.get(item.id);
    const state =
      live?.available === true
        || (live?.available == null && live?.inventory?.configured)
        ? 'ready'
        : live?.code === 'unsupported_host'
          ? 'unsupported'
          : item.state;
    return { ...item, state, enhanced: live };
  });
}

function groupStats(
  items: Array<{ visualState: VisualState }>,
  includePartialAsInstalled = false,
): GroupStats {
  return {
    total: items.length,
    available: items.filter((item) => item.visualState === 'available').length,
    installed: items.filter(
      (item) => (
        item.visualState === 'available'
        || (includePartialAsInstalled && item.visualState === 'partial')
      ),
    ).length,
    installing: items.filter((item) => item.visualState === 'installing').length,
    notInstalled: items.filter(
      (item) => (
        item.visualState === 'notInstalled'
        || item.visualState === 'needsAction'
        || item.visualState === 'detected'
        || item.visualState === 'downloaded'
        || (!includePartialAsInstalled && item.visualState === 'partial')
        || item.visualState === 'unsupported'
      ),
    ).length,
    failed: items.filter((item) => item.visualState === 'failed').length,
  };
}

function statusLabel(state: VisualState, t: (key: string) => string): string {
  return t(`reaSetup.visualState.${state}`);
}

export function ReaSetupPage({ onUseRea }: ReaSetupPageProps) {
  const { t } = useTranslation();
  const activeJobStorage = typeof window === 'undefined' ? null : window.localStorage;
  const initialActiveJobRef = useRef<ReaSetupJob | null | undefined>(undefined);
  if (initialActiveJobRef.current === undefined) {
    initialActiveJobRef.current = readPersistedActiveJob(activeJobStorage);
  }
  const cachedScanRef = useRef<ReaSetupScan | null>(readCachedScan());
  const [scan, setScan] = useState<ReaSetupScan | null>(cachedScanRef.current);
  const [job, setJobState] = useState<ReaSetupJob | null>(initialActiveJobRef.current);
  const [enhancedResult, setEnhancedResult] = useState<ReaSetupInstallResult | null>(null);
  const [loading, setLoading] = useState(
    cachedScanRef.current === null && initialActiveJobRef.current === null,
  );
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [clock, setClock] = useState(Date.now());
  const [scanStartedAt, setScanStartedAt] = useState(Date.now());
  const [decisionPending, setDecisionPending] = useState(false);
  const [decisionModalOpen, setDecisionModalOpen] = useState(false);
  const [baseExpanded, setBaseExpanded] = useState(true);
  const [enhancedExpanded, setEnhancedExpanded] = useState(true);
  const [explaining, setExplaining] = useState<CapabilityCardView | null>(null);
  const [configuring, setConfiguring] = useState<ReaSetupEnhancedItem | null>(null);
  const [configureValues, setConfigureValues] = useState<Record<string, string>>({});
  const [configureSaving, setConfigureSaving] = useState(false);
  const [copiedCommand, setCopiedCommand] = useState('');
  const [detailsOpen, setDetailsOpen] = useState(false);
  const baseStartingRef = useRef(false);
  const enhancedStartingRef = useRef(false);
  const scanRef = useRef<ReaSetupScan | null>(scan);
  const refreshInFlightRef = useRef(false);
  const stableRemainingRef = useRef<{ jobId: string; seconds: number } | null>(null);
  const displayedRemainingRef = useRef<{
    jobId: string;
    seconds: number;
    updatedAt: number;
  } | null>(null);
  const resumedJobRef = useRef(false);

  const setJob = useCallback((next: ReaSetupJob | null) => {
    setJobState(next);
    persistActiveJob(activeJobStorage, next);
  }, [activeJobStorage]);

  const installing = job?.status === 'queued' || job?.status === 'running';
  const baseReady = scan?.ready === true;
  const mergedPlan = useMemo(
    () => mergePlan(scan?.enhanced_plan ?? [], scan?.enhanced ?? []),
    [scan?.enhanced, scan?.enhanced_plan],
  );
  const resultById = useMemo(() => {
    const results = new Map(
      Object.entries(job?.component_results ?? {}).map(([id, item]) => [id, item]),
    );
    for (const item of enhancedResult?.enhanced_install_results ?? []) {
      results.set(item.id, item);
    }
    return results;
  }, [enhancedResult?.enhanced_install_results, job?.component_results]);

  const applyScan = useCallback((next: ReaSetupScan) => {
    scanRef.current = next;
    setScan(next);
    writeCachedScan(next);
  }, []);

  const refresh = useCallback(async (scope: 'all' | GroupKind = 'all') => {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    const hasSnapshot = scanRef.current !== null;
    setError('');
    setLoading(!hasSnapshot && initialActiveJobRef.current === null);
    setRefreshing(hasSnapshot);
    setScanStartedAt(Date.now());
    try {
      const next = await reaSetupApi.status(scope);
      const previous = scanRef.current;
      applyScan(
        scope === 'base' && previous && next.ready
          ? {
              ...previous,
              ...next,
              enhanced: previous.enhanced,
              enhanced_plan: previous.enhanced_plan,
              capability_cards: previous.capability_cards,
              rea_inventory_diagnostics: previous.rea_inventory_diagnostics,
            }
          : next,
      );
      if (!next.ready) {
        setEnhancedResult(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      refreshInFlightRef.current = false;
      setLoading(false);
      setRefreshing(false);
    }
  }, [applyScan]);

  const pollJob = useCallback(async (initial: ReaSetupJob): Promise<ReaSetupJob> => {
    let current = initial;
    setJob(current);
    while (current.status === 'queued' || current.status === 'running') {
      await new Promise((resolve) => window.setTimeout(resolve, 750));
      let retry = 0;
      for (;;) {
        try {
          current = await reaSetupApi.progress(current.job_id);
          break;
        } catch (cause) {
          retry += 1;
          if (retry >= 3) throw cause;
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
        }
      }
      setJob(current);
    }
    return current;
  }, [setJob]);

  const applyCompletedJob = useCallback((current: ReaSetupJob) => {
    if (current.status === 'cancelled') {
      setError(t('reaSetup.installCancelled'));
    } else if (current.result?.scan) {
      if (current.kind === 'enhanced') {
        setEnhancedResult(current.result);
      }
      applyScan(current.result.scan);
      if (current.kind === 'base' && current.result.base_ready) {
        setDecisionPending(true);
        setDecisionModalOpen(true);
      }
    } else if (current.error) {
      setError(current.error);
    }
  }, [applyScan, t]);

  const installEnhanced = useCallback(async (components?: string[]) => {
    if (enhancedStartingRef.current || installing) return;
    enhancedStartingRef.current = true;
    setBaseExpanded(true);
    setEnhancedExpanded(true);
    setDecisionPending(false);
    setDecisionModalOpen(false);
    setError('');
    try {
      const initial = await reaSetupApi.installEnhanced(components);
      const current = await pollJob(initial);
      applyCompletedJob(current);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      enhancedStartingRef.current = false;
    }
  }, [applyCompletedJob, installing, pollJob]);

  const installBase = useCallback(async () => {
    if (baseStartingRef.current || installing) return;
    baseStartingRef.current = true;
    setBaseExpanded(true);
    setEnhancedExpanded(true);
    setError('');
    setEnhancedResult(null);
    setDecisionPending(false);
    setDecisionModalOpen(false);
    try {
      const initial = await reaSetupApi.installBase();
      const current = await pollJob(initial);
      applyCompletedJob(current);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      baseStartingRef.current = false;
    }
  }, [applyCompletedJob, installing, pollJob]);

  useEffect(() => {
    if (cachedScanRef.current === null && initialActiveJobRef.current === null) {
      void refresh();
    }
  }, [refresh]);

  useEffect(() => {
    const persisted = initialActiveJobRef.current;
    if (!persisted || resumedJobRef.current) return;
    resumedJobRef.current = true;
    void pollJob(persisted)
      .then(applyCompletedJob)
      .catch((cause) => {
        setError(cause instanceof Error ? cause.message : String(cause));
      });
  }, [applyCompletedJob, pollJob]);

  useEffect(() => {
    if (!loading && !refreshing && !installing) return undefined;
    const timer = window.setInterval(() => setClock(Date.now()), 500);
    return () => window.clearInterval(timer);
  }, [installing, loading, refreshing]);

  const promptEnhancedInstall = useCallback(() => {
    setError('');
    setDecisionPending(true);
    setDecisionModalOpen(true);
  }, []);

  const skipEnhanced = useCallback(() => {
    setDecisionPending(false);
    setDecisionModalOpen(false);
  }, []);

  const cancelInstallation = useCallback(async () => {
    if (!job || !installing) return;
    try {
      const current = await reaSetupApi.cancel(job.job_id);
      setJob(current);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [installing, job]);

  const openConfigure = useCallback((item: ReaSetupEnhancedItem) => {
    if (item.code === 'unsupported_host' || item.inventory?.state === 'unsupported') {
      return;
    }
    setConfiguring(item);
    const discoveredEnv = item.inventory?.env ?? {};
    const nextValues: Record<string, string> = {};
    for (const input of item.assisted_setup?.inputs ?? []) {
      const value = discoveredEnv[input.env_key];
      if (value) nextValues[input.id] = value;
    }
    setConfigureValues(nextValues);
    setCopiedCommand('');
    setError('');
  }, []);

  const chooseInputPath = useCallback(async (input: ReaAssistedInput) => {
    try {
      const selected =
        input.type === 'directory'
          ? await reaSetupApi.selectDirectory(input.label)
          : await reaSetupApi.selectFile(input.label);
      if (!selected.cancelled && selected.path) {
        setConfigureValues((current) => ({ ...current, [input.id]: selected.path || '' }));
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  const copyCommand = useCallback(async (commandId: string, content: string) => {
    try {
      await navigator.clipboard.writeText(content);
      setCopiedCommand(commandId);
      window.setTimeout(
        () => setCopiedCommand((current) => (current === commandId ? '' : current)),
        1800,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, []);

  const configure = useCallback(async () => {
    if (!configuring) return;
    setConfigureSaving(true);
    setError('');
    try {
      const result = await reaSetupApi.configure(configuring.id, configureValues);
      applyScan(result.scan);
      setConfiguring(null);
      setConfigureValues({});
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setConfigureSaving(false);
    }
  }, [applyScan, configureValues, configuring]);

  const baseCardVisualState = useCallback(
    (id: string, ok: boolean): VisualState => {
      const stepIds =
        id === 'node_npx'
          ? ['node_npx', 'node_runtime_download']
          : id === 'rea_dependencies'
            ? ['rea_package']
            : ['verify_mcp'];
      const relevant = (job?.steps ?? []).filter((step) => stepIds.includes(step.id));
      if (relevant.some((step) => step.status === 'failed')) return 'failed';
      if (relevant.some((step) => step.status === 'running')) return 'installing';
      if (relevant.length > 0 && relevant.every((step) => step.status === 'succeeded')) {
        return 'available';
      }
      return ok ? 'available' : 'notInstalled';
    },
    [job?.steps],
  );

  const componentVisualState = useCallback(
    (item: PlanWithEnhanced): VisualState => {
      const result = resultById.get(item.id);
      if (result) {
        if (result.state === 'installed' || result.state === 'reused') return 'available';
        if (result.state === 'needs_action') return 'needsAction';
        if (result.state === 'unsupported') return 'unsupported';
        return 'failed';
      }
      const steps = (job?.steps ?? []).filter((step) =>
        (step.id || '').startsWith(`component_${item.id}_`),
      );
      if (steps.some((step) => step.status === 'failed')) return 'failed';
      if (
        steps.some(
          (step) => step.status === 'running' || (step.status === 'succeeded' && !steps.every((candidate) => candidate.status !== 'pending')),
        )
      ) {
        return 'installing';
      }
      const inventoryState = item.enhanced?.inventory?.state ?? item.enhanced?.local_state;
      if (inventoryState === 'unsupported') return 'unsupported';
      if (inventoryState === 'partial') return 'partial';
      if (inventoryState === 'downloaded' || inventoryState === 'cached') return 'downloaded';
      if (inventoryState === 'discovered') return 'detected';
      if (inventoryState === 'configured') {
        return item.enhanced?.available === false || item.state === 'needs_action'
          ? 'needsAction'
          : 'available';
      }
      if (item.state === 'ready') return 'available';
      if (item.state === 'needs_action') return 'needsAction';
      if (item.state === 'unsupported') return 'unsupported';
      return 'notInstalled';
    },
    [job?.steps, resultById],
  );

  const baseDisplayItems = (scan?.base ?? []).map((item) => ({
    ...item,
    visualState: baseCardVisualState(item.id, item.ok),
  }));
  const enhancedDisplayItems = mergedPlan.map((item) => ({
    ...item,
    visualState: componentVisualState(item),
  }));
  const capabilityCardState = (state: string): VisualState => {
    if (state === 'available') return 'available';
    if (state === 'installing') return 'installing';
    if (state === 'partial') return 'partial';
    if (state === 'needs_action') return 'needsAction';
    if (state === 'unsupported') return 'unsupported';
    if (state === 'failed') return 'failed';
    return 'notInstalled';
  };
  const liveCardById = new Map((scan?.capability_cards ?? []).map((card) => [card.id, card]));
  const enhancedScanPending = shouldDeferEnhancedDetection(
    job,
    scan?.capability_cards?.length ?? 0,
  );
  const capabilityCards: CapabilityCardView[] = CAPABILITY_SPECS.map((spec) => {
    const memberItems = spec.memberIds
      .map((id) => enhancedDisplayItems.find((item) => item.id === id))
      .filter((item): item is (typeof enhancedDisplayItems)[number] => Boolean(item));
    const liveCard = liveCardById.get(spec.id);
    const liveMemberById = new Map(
      (liveCard?.members ?? []).map((member) => [member.id, member]),
    );
    const members: CapabilityMemberView[] = spec.memberIds.map((id) => {
      const item = memberItems.find((candidate) => candidate.id === id);
      const liveMember = liveMemberById.get(id);
    const memberState = enhancedScanPending
        ? 'installing'
        : item?.visualState
          || (liveMember ? capabilityCardState(liveMember.state) : 'notInstalled');
      return {
        id,
        label: liveMember?.label || item?.label || MEMBER_FALLBACK_LABELS[id] || id,
        visualState: memberState,
        reason: t(`reaSetup.memberState.${memberState}`),
      };
    });
    let visualState: VisualState;
    if (enhancedScanPending) {
      visualState = 'installing';
    } else if (memberItems.some((item) => item.visualState === 'failed')) {
      visualState = 'failed';
    } else if (memberItems.some((item) => item.visualState === 'installing')) {
      visualState = 'installing';
    } else if (memberItems.length === 0) {
      visualState = liveCard
        ? capabilityCardState(liveCard.state)
        : scan?.platform_id?.startsWith('macos')
          ? 'available'
          : 'unsupported';
    } else if (memberItems.every((item) => item.visualState === 'unsupported')) {
      visualState = 'unsupported';
    } else if (memberItems.every((item) => item.visualState === 'available')) {
      visualState = 'available';
    } else if (memberItems.some((item) => item.visualState === 'available')) {
      visualState = 'partial';
    } else if (liveCard) {
      visualState = capabilityCardState(liveCard.state);
    } else if (memberItems.some((item) => item.visualState === 'needsAction')) {
      visualState = 'needsAction';
    } else {
      visualState = 'notInstalled';
    }
    if (installing && job?.kind === 'enhanced' && visualState !== 'available' && visualState !== 'failed') {
      visualState = 'installing';
    }
    const actionMember = memberItems.find(
      (item) => (
        item.visualState === 'needsAction'
        || item.visualState === 'detected'
        || item.visualState === 'downloaded'
      )
      && item.enhanced?.assisted_setup
    );
    return {
      id: spec.id,
      label: liveCard?.label || t(spec.labelKey),
      description: liveCard?.description || t(spec.descriptionKey),
      usage: t(spec.hoverKey),
      stateHint: t(`reaSetup.capabilityHint.${visualState}`),
      hoverText: `${t(spec.hoverKey)}\n${t(`reaSetup.capabilityHint.${visualState}`)}`,
      visualState,
      actionItem: actionMember?.enhanced,
      members,
    };
  });
  const baseStats = groupStats(baseDisplayItems);
  const enhancedStats = groupStats(capabilityCards, true);
  const componentSteps = (job?.steps ?? []).filter((step) => (step.id || '').startsWith('component_'));
  const componentStepGroups = new Map<string, ReaSetupStep[]>();
  componentSteps.forEach((step) => {
    const match = (step.id || '').match(
      /^component_(.+)_(planning|connect|download|verify|install|verified|cached|complete|failed)$/,
    );
    if (!match) return;
    const existing = componentStepGroups.get(match[1]) ?? [];
    componentStepGroups.set(match[1], [...existing, step]);
  });
  const autoPlanSize = (job?.plan ?? []).filter((item) => item.auto_install).length;
  const componentCount = job?.kind === 'base'
    ? baseDisplayItems.length
    : (componentStepGroups.size || autoPlanSize || enhancedStats.total);
  const baseStepById = new Map((job?.steps ?? []).map((step) => [step.id, step]));
  const finalVerificationStep = baseStepById.get('enhanced_scan');
  const hasFinalVerificationItem =
    job?.kind === 'enhanced' && finalVerificationStep !== undefined;
  const progressTotalComponents = componentCount + (hasFinalVerificationItem ? 1 : 0);
  const completedComponentGroups = job?.kind === 'base'
    ? ['node_npx', 'rea_package', 'verify_mcp'].filter(
        (stepId) => baseStepById.get(stepId)?.status === 'succeeded',
      ).length
    : [...componentStepGroups.values()].filter(
        (steps) => steps.every(
          (step) => step.status === 'succeeded' || step.status === 'skipped',
        ),
      ).length;
  const progressCompletedComponents = completedComponentGroups
    + (
      hasFinalVerificationItem
      && (
        finalVerificationStep?.status === 'succeeded'
        || finalVerificationStep?.status === 'skipped'
      )
        ? 1
        : 0
    );
  const activeStep =
    (job?.steps ?? []).find(
      (step) => step.id === job?.current_step_id && step.status === 'running',
    )
    || (job?.steps ?? []).find((step) => step.status === 'running')
    || (job?.steps ?? []).find((step) => step.id === job?.current_step_id);
  const progressHeadline = activeStep?.label || job?.message || t('reaSetup.installingComponents');
  const progressDetail = messageWithoutEta(
    job?.message || t('reaSetup.componentCountHint', { count: progressTotalComponents }),
  );
  const elapsedSeconds = job ? Math.max(0, Math.floor((clock - job.started_at * 1000) / 1000)) : 0;
  const rawPercent = Math.max(0, Math.min(100, Number(job?.percent) || 0));
  const displayPercent = job?.status === 'succeeded'
    ? 100
    : Math.min(99, Math.floor(rawPercent));
  const weightedRemainingSeconds =
    job?.estimated_seconds_min != null
      ? displayPercent > 0
        ? Math.max(
          1,
          Math.round(
            Math.max(0, job.estimated_seconds_min - elapsedSeconds)
            * ((100 - displayPercent) / displayPercent),
          ),
        )
        : Math.max(0, job.estimated_seconds_min - elapsedSeconds)
      : null;
  const previousRemainingSeconds =
    job && stableRemainingRef.current?.jobId === job.job_id
      ? stableRemainingRef.current.seconds
      : null;
  const downloadRemainingSeconds = estimateRemainingSeconds(job, previousRemainingSeconds);
  const paceRemainingSeconds = estimatePaceRemainingSeconds(
    job,
    displayPercent,
    elapsedSeconds,
  );
  const plannedRemainingSeconds = displayPercent >= 10
    ? paceRemainingSeconds ?? weightedRemainingSeconds
    : weightedRemainingSeconds;
  const candidateRemainingSeconds = downloadRemainingSeconds
    ? Math.max(
        downloadRemainingSeconds,
        plannedRemainingSeconds ?? previousRemainingSeconds ?? 0,
      )
    : plannedRemainingSeconds ?? previousRemainingSeconds;
  const remainingSeconds = stabilizeRemainingSeconds(
    job,
    candidateRemainingSeconds,
    previousRemainingSeconds,
  );
  if (job && remainingSeconds != null) {
    stableRemainingRef.current = { jobId: job.job_id, seconds: remainingSeconds };
  } else {
    stableRemainingRef.current = null;
  }
  const displayedRemaining = job
    ? selectDisplayedRemainingSeconds(
        job.job_id,
        remainingSeconds,
        displayedRemainingRef.current,
        Date.now(),
      )
    : null;
  displayedRemainingRef.current = displayedRemaining;
  const displayedRemainingSeconds = displayedRemaining?.seconds ?? null;
  const remainingTime =
    displayedRemainingSeconds == null ? null : formatProgressDuration(displayedRemainingSeconds);
  const enhancedEstimateRange = t('reaSetup.enhancedEstimateFallback');
  const progressHint =
    decisionPending || job?.kind === 'enhanced'
      ? t('reaSetup.enhancedProgressHint', { range: enhancedEstimateRange })
      : t('reaSetup.progressHint');
  const showProgress = Boolean(installing || decisionPending);
  const showWorkspace = Boolean(!loading && scan && (baseReady || job?.status === 'succeeded'));
  const assistedInputs = configuring?.assisted_setup?.inputs ?? [];
  const assistedRequiredMissing = assistedInputs.some(
    (input) => input.required && !configureValues[input.id]?.trim(),
  );
  const assistedRequiredAny =
    configuring?.assisted_setup?.required_any?.some((inputId) =>
      configureValues[inputId]?.trim(),
    ) ?? true;
  const configureDisabled =
    configureSaving ||
    configuring?.code === 'unsupported_host' ||
    assistedRequiredMissing ||
    !assistedRequiredAny;
  const hasSensitiveInput = assistedInputs.some(
    (input) => input.type === 'json' || input.type === 'text',
  );
  const viewState = loading
    ? 'loading'
    : showProgress
      ? 'progress'
      : showWorkspace
        ? 'workspace'
        : 'empty';

  const baseItemLabel = (id: string): string => {
    if (id === 'node_npx') return t('reaSetup.baseLabel.node');
    if (id === 'rea_dependencies') return t('reaSetup.baseLabel.runtime');
    if (id === 'rea_mcp') return t('reaSetup.baseLabel.mcp');
    return '';
  };

  const renderCapabilityCard = (item: CapabilityCardView, compact: boolean) => {
    const singleMember = item.members.length <= 1;
    const showConfigure = singleMember && (
      item.visualState === 'needsAction' || item.visualState === 'detected'
    );
    const showGuide =
      item.visualState === 'partial'
      || (!singleMember && (
        item.visualState === 'needsAction' || item.visualState === 'detected'
      ))
      || item.visualState === 'unsupported'
      || item.visualState === 'failed';
    return (
      <article
        className={`rea-component-card${compact ? ' rea-component-card--compact' : ''}`}
        key={item.id}
        data-testid="rea-setup-capability-card"
        data-variant={item.id}
        data-state={item.visualState}
        data-actionable={showConfigure || showGuide ? 'true' : 'false'}
        title={item.hoverText}
      >
        <div className="rea-component-card__top">
          <span className="rea-component-card__identity">
            <strong>{item.label}</strong>
            <span className={`rea-status is-${item.visualState}`}>
              <i />
              {statusLabel(item.visualState, t)}
            </span>
          </span>
          <span className="rea-component-card__actions">
            {showConfigure && (
              <button
                type="button"
                className="rea-text-action rea-component-card__action"
                disabled={!item.actionItem}
                onClick={() => {
                  if (item.actionItem) openConfigure(item.actionItem);
                }}
              >
                {t('reaSetup.configure')}
              </button>
            )}
            {showGuide && (
              <button
                type="button"
                className="rea-text-action rea-component-card__action"
                onClick={() => setExplaining(item)}
              >
                {t('reaSetup.viewGuide')}
              </button>
            )}
          </span>
        </div>
        {!compact && (
          <div className="rea-component-card__footer">
            <span>{item.description}</span>
          </div>
        )}
      </article>
    );
  };

  const renderGroup = (
    kind: GroupKind,
    title: string,
    stats: GroupStats,
    expanded: boolean,
    onToggle: () => void,
    children: ReactNode,
  ) => {
    const showBadge = !installing && stats.installed > 0;
    const badgeState = stats.available === stats.total ? 'complete' : 'partial';
    return (
      <section className="rea-group">
        <div className="rea-group__header">
          <button
            type="button"
            className="rea-group__toggle"
            aria-expanded={expanded}
            onClick={onToggle}
          >
            <span className="rea-group__title">
              <strong>{title}</strong>
              {showBadge && (
                <span className={`rea-group__badge is-${badgeState}`}>
                  {badgeState === 'complete'
                    ? t('reaSetup.groupState.complete')
                    : t('reaSetup.groupState.partial')}
                </span>
              )}
            </span>
          </button>
        <span className="rea-group__stats">
          <span>{t('reaSetup.stats.all')}<b>{stats.total}</b></span>
          <span>{t('reaSetup.stats.installed')}<b>{stats.installed}</b></span>
          <span>{t('reaSetup.stats.installing')}<b>{stats.installing}</b></span>
          <span>{t('reaSetup.stats.uninstalled')}<b>{stats.notInstalled}</b></span>
          <span>{t('reaSetup.stats.failed')}<b>{stats.failed}</b></span>
          <span className="rea-group__divider" />
          <button
            type="button"
            className="rea-group__check"
            disabled={loading || refreshing || installing}
            onClick={() => void refresh(kind)}
            data-testid={`rea-check-${kind}`}
          >
            <img className="rea-group__check-icon" src={detectArt} alt="" aria-hidden />
            {refreshing ? t('reaSetup.checking') : t('reaSetup.stats.check')}
          </button>
          <button
            type="button"
            className="rea-group__expand"
            aria-label={expanded ? t('reaSetup.collapse') : t('reaSetup.expand')}
            aria-expanded={expanded}
            onClick={onToggle}
          >
            <span className={`rea-group__chevron ${expanded ? 'is-open' : ''}`}>⌄</span>
          </button>
        </span>
      </div>
      {expanded && <div className="rea-group__body">{children}</div>}
      {kind === 'enhanced' && expanded && (
        <div className="rea-group__categories">
          <div className="rea-category">
            <h4>{t('reaSetup.groups.native')}</h4>
            <div className="rea-component-grid">
              {capabilityCards.slice(0, 4).map((item) => renderCapabilityCard(item, true))}
            </div>
          </div>
          <div className="rea-component-grid">
            {capabilityCards.slice(4).map((item) => renderCapabilityCard(item, false))}
          </div>
        </div>
      )}
      </section>
    );
  };

  return (
    <div className="rea-page" data-testid="rea-setup-page" data-view={viewState}>
      <div className="rea-page__inner">
        <header className="rea-page__header">
          <h1>{t('reaSetup.pageTitle')}</h1>
          <p>{t('reaSetup.pageSubtitle')}</p>
        </header>

        {refreshing && (
          <div className="rea-refresh-indicator" role="status">
            <LoadingSpinner />
            <span>{t('reaSetup.checkingEnvironment')}</span>
          </div>
        )}

        {loading && (
          <div className="rea-loading" data-testid="rea-setup-loading">
            <LoadingSpinner />
            <span>{t('reaSetup.scanning')} · {t('reaSetup.elapsed', { count: Math.max(0, Math.floor((clock - scanStartedAt) / 1000)) })}</span>
          </div>
        )}

        {!loading && !showWorkspace && !showProgress && (
          <div className="rea-empty" data-testid="rea-setup-empty-state">
            <h2>{t('reaSetup.emptyTitle')}</h2>
            <div className="rea-empty__actions">
              <button
                type="button"
                className="rea-button rea-button--outline"
                onClick={() => void installBase()}
                data-testid="rea-setup-install-one-click"
              >
                {t('reaSetup.installOneClick')}
              </button>
            </div>
            <div className="rea-step-cards" data-testid="rea-setup-step-cards">
              <div className="rea-step-card">
                <img src={stepInstallArt} alt="" />
              </div>
              <div className="rea-step-card">
                <img src={stepEnhanceArt} alt={t('reaSetup.guide.enhanceTitle')} />
              </div>
              <div className="rea-step-card">
                <img src={stepCallArt} alt={t('reaSetup.guide.callTitle')} />
              </div>
            </div>
          </div>
        )}

        {showProgress && (
          <section className="rea-progress-card" aria-live="polite" data-testid="rea-setup-progress">
            <div className="rea-progress-card__head">
              <div className="rea-progress-card__identity">
                <span className="rea-progress-card__icon">
                  <svg viewBox="0 0 32 32" aria-hidden>
                    <path d="M9 8.5 13.5 6l6 3.5v7L14 20l-5-3.5v-8Z" fill="#fff" />
                    <path d="m13 13 3 4 5-6" fill="none" stroke="#222" strokeWidth="1.5" strokeLinecap="round" />
                  </svg>
                </span>
                <div>
                  <strong>{t('reaSetup.preparingEnvironment')}</strong>
                  <span>{progressHint}</span>
                </div>
              </div>
              {installing ? (
                <button type="button" className="rea-button rea-button--outline rea-button--small" onClick={() => void cancelInstallation()}>
                  {t('reaSetup.cancelInstall')}
                </button>
              ) : decisionPending ? (
                <button type="button" className="rea-button rea-button--outline rea-button--small" disabled>
                  {t('reaSetup.cancelInstall')}
                </button>
              ) : null}
            </div>
            <div className="rea-progress-card__body">
              <div className="rea-progress-card__summary-title">
                <strong>{progressHeadline}</strong>
                <span>{progressDetail}</span>
              </div>
              <div className="rea-progress-card__bar-row">
                <span className="rea-progress-card__percent">
                  {t('reaSetup.installProgress')} <b>{Math.round(displayPercent)}%</b>
                </span>
                <div className="rea-progress-card__numbers">
                  <span className="rea-progress-card__metric">
                    <span>{t('reaSetup.progressMetrics.completed')}</span>
                    <b>{progressCompletedComponents}/{progressTotalComponents}</b>
                    <span className="rea-progress-card__metric-unit">
                      {t('reaSetup.progressMetrics.items')}
                    </span>
                  </span>
                  <i />
                  {remainingTime ? (
                    <span className="rea-progress-card__metric">
                      <span>{t('reaSetup.progressMetrics.remaining')}</span>
                      <b>{remainingTime.value}</b>
                      <span className="rea-progress-card__metric-unit">
                        {remainingTime.unit === 'minutes'
                          ? t('reaSetup.progressMetrics.minutes')
                          : t('reaSetup.progressMetrics.seconds')}
                      </span>
                    </span>
                  ) : (
                    <span className="rea-progress-card__metric-label">
                      {t('reaSetup.estimating')}
                    </span>
                  )}
                </div>
              </div>
              <div
                className="rea-progress-card__bar"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(displayPercent)}
              >
                <span style={{ width: `${Math.max(2, displayPercent)}%` }} />
              </div>
            </div>
          </section>
        )}

        {!loading && (showProgress || showWorkspace) && (
          <section className="rea-install-area">
            <h2>{showProgress ? t('reaSetup.installComponents') : t('reaSetup.environmentTitle')}</h2>
            {!showProgress && (enhancedStats.notInstalled > 0 || enhancedStats.failed > 0) && (
              <div className="rea-section-action">
                <button
                  type="button"
                  className="rea-button rea-button--outline rea-button--small"
                  onClick={promptEnhancedInstall}
                >
                  {t('reaSetup.continueInstall')}
                </button>
              </div>
            )}
            {renderGroup(
              'base',
              t('reaSetup.baseGroup'),
              baseStats,
              baseExpanded,
              () => setBaseExpanded((value) => !value),
              (
                <>
                  <div className="rea-component-grid rea-component-grid--base">
                    {baseDisplayItems.map((item) => (
                      <article className="rea-component-card" key={item.id} data-state={item.visualState}>
                        <div className="rea-component-card__top">
                          <strong>{baseItemLabel(item.id)}</strong>
                          <span className={`rea-status is-${item.visualState}`}>
                            <i />
                            {statusLabel(item.visualState, t)}
                          </span>
                        </div>
                        <div className="rea-component-card__footer">
                          <span>
                            {item.id === 'node_npx'
                              ? t('reaSetup.cardDetail.node', {
                                  version: item.version || '—',
                                  npmVersion: item.npm_version || 'npm',
                                })
                              : item.id === 'rea_dependencies'
                                ? t('reaSetup.cardDetail.runtime', { version: item.version || scan?.rea_version || REA_VERSION_FALLBACK })
                                : t('reaSetup.cardDetail.mcpVerified')}
                          </span>
                          {item.id === 'rea_mcp' && item.ok && (
                            <button type="button" className="rea-text-action" onClick={() => setDetailsOpen(true)}>
                              {t('reaSetup.viewDetails')}
                            </button>
                          )}
                        </div>
                      </article>
                    ))}
                  </div>
                </>
              ),
            )}
            {renderGroup(
              'enhanced',
              showProgress ? t('reaSetup.enhancedGroupOptional') : t('reaSetup.enhancedGroup'),
              enhancedStats,
              enhancedExpanded,
              () => setEnhancedExpanded((value) => !value),
              null,
            )}
          </section>
        )}

        {!loading && baseReady && !showProgress && (
          <section className="rea-task-guide">
            <div className="rea-task-guide__head">
              <h2>{t('reaSetup.guide.title')}</h2>
              <button type="button" className="rea-button rea-button--dark rea-button--small" onClick={() => onUseRea(t('reaSetup.guide.actionPrompt'))}>
                {t('reaSetup.goUse')}
              </button>
            </div>
            <div className="rea-guide-grid">
              {[
                { type: 'target' as const, icon: guideTargetArt, title: t('reaSetup.guide.step1'), desc: t('reaSetup.guide.step1Desc'), prompt: '' },
                { type: 'mcp' as const, icon: guideMcpArt, title: t('reaSetup.guide.step2'), desc: t('reaSetup.guide.step2Desc'), prompt: t('reaSetup.guide.actionPrompt') },
                { type: 'confirm' as const, icon: guideConfirmArt, title: t('reaSetup.guide.step3'), desc: t('reaSetup.guide.step3Desc'), prompt: t('reaSetup.guide.prompt1') },
              ].map((item) => (
                <button type="button" className="rea-guide-card" key={item.type} onClick={() => onUseRea(item.prompt)}>
                  <img className="rea-guide-card__icon" src={item.icon} alt="" />
                  <strong>{item.title}</strong>
                  <span>{item.desc}</span>
                </button>
              ))}
            </div>
            <div className="rea-prompt-row">
              {[
                t('reaSetup.guide.prompt1'),
                t('reaSetup.guide.prompt2'),
                t('reaSetup.guide.prompt3'),
              ].map((prompt) => (
                <button type="button" className="rea-prompt-chip" key={prompt} onClick={() => onUseRea(prompt)}>
                  <span className="rea-prompt-chip__brand">
                    <SkillIcon className="rea-prompt-chip__skill-icon" aria-hidden />
                    <b>rea</b>
                  </span>
                  <span className="rea-prompt-chip__text">{prompt}</span>
                  <span className="rea-prompt-chip__arrow">↗</span>
                </button>
              ))}
            </div>
          </section>
        )}

        {error && (
          <div className="rea-error" role="alert" data-testid="rea-setup-error">
            <strong>{t('reaSetup.errorTitle')}</strong>
            <span>{error}</span>
            <button type="button" className="rea-text-action" onClick={() => void refresh()}>
              {t('reaSetup.stats.check')}
            </button>
          </div>
        )}

      </div>

      {decisionModalOpen && decisionPending && (
        <div className="rea-modal-layer" role="dialog" aria-modal="true" aria-labelledby="rea-decision-title">
          <div className="rea-decision-modal">
            <button type="button" className="rea-modal-close" onClick={skipEnhanced} aria-label={t('common.close')}>×</button>
            <span className="rea-decision-modal__check">✓</span>
            <h2 id="rea-decision-title">{t('reaSetup.decisionTitle')}</h2>
            <p>{t('reaSetup.decisionDescription')}</p>
            <div className="rea-decision-modal__actions">
              <button type="button" className="rea-button rea-button--outline rea-button--small" onClick={skipEnhanced}>
                {t('reaSetup.skipEnhance')}
              </button>
              <button type="button" className="rea-button rea-button--dark rea-button--small" onClick={() => void installEnhanced()}>
                {t('reaSetup.continueEnhance')}
              </button>
            </div>
          </div>
        </div>
      )}

      {explaining && (
        <div className="rea-modal-layer" role="dialog" aria-modal="true" aria-labelledby="rea-explanation-title">
          <div className="rea-explanation-modal">
            <button
              type="button"
              className="rea-modal-close"
              onClick={() => setExplaining(null)}
              aria-label={t('common.close')}
            >
              ×
            </button>
            <span className={`rea-status is-${explaining.visualState}`}>
              <i />
              {statusLabel(explaining.visualState, t)}
            </span>
            <h2 id="rea-explanation-title">{explaining.label}</h2>
            <p>{explaining.usage}</p>
            <p className="rea-explanation-modal__hint">{explaining.stateHint}</p>
            <div className="rea-explanation-members">
              <h3>{t('reaSetup.memberDetails')}</h3>
              <ul>
                {explaining.members.map((member) => (
                  <li key={member.id} data-state={member.visualState}>
                    <span className={`rea-status is-${member.visualState}`}>
                      <i />
                      {member.label}
                    </span>
                    <span>{member.reason}</span>
                  </li>
                ))}
              </ul>
            </div>
            <button
              type="button"
              className="rea-button rea-button--dark rea-button--small"
              onClick={() => setExplaining(null)}
            >
              {t('reaSetup.gotIt')}
            </button>
          </div>
        </div>
      )}

      {detailsOpen && (
        <div className="rea-modal-layer" role="dialog" aria-modal="true" aria-label={t('reaSetup.mcpDetailsTitle')}>
          <div className="rea-details-modal">
            <button type="button" className="rea-modal-close" onClick={() => setDetailsOpen(false)} aria-label={t('common.close')}>×</button>
            <h2>{t('reaSetup.mcpDetailsTitle')}</h2>
            <dl>
              <dt>REA</dt><dd>{scan?.rea_version ?? REA_VERSION_FALLBACK}</dd>
              <dt>{t('reaSetup.tools')}</dt><dd>{scan?.base.find((item) => item.id === 'rea_mcp')?.tool_count ?? 0}</dd>
              <dt>Node.js</dt><dd>{scan?.base.find((item) => item.id === 'node_npx')?.version ?? '—'}</dd>
              <dt>{t('reaSetup.state')}</dt><dd>{scan?.base.find((item) => item.id === 'rea_mcp')?.state ?? '—'}</dd>
              <dt>{t('reaSetup.inventoryGroups')}</dt>
              <dd>
                {scan?.rea_inventory_diagnostics?.group_count ?? '—'}
                {' / '}
                {scan?.rea_inventory_diagnostics?.availability_tool_count ?? '—'}
              </dd>
            </dl>
            {scan?.rea_inventory_diagnostics?.inventory_warning && (
              <p className="rea-config-notice" data-testid="rea-setup-inventory-warning">
                {scan.rea_inventory_diagnostics.inventory_warning}
              </p>
            )}
            <button type="button" className="rea-button rea-button--dark rea-button--small" onClick={() => setDetailsOpen(false)}>
              {t('reaSetup.close')}
            </button>
          </div>
        </div>
      )}

      {configuring?.assisted_setup && (
        <div className="rea-modal-layer" role="dialog" aria-modal="true" aria-label={configuring.label}>
          <div className="rea-config-modal">
            <button type="button" className="rea-modal-close" onClick={() => setConfiguring(null)} aria-label={t('common.close')}>×</button>
            <h2>
              {t('reaSetup.configureTitle', {
                name: capabilityCards.find((item) => item.id === configuring.id)?.label
                  ?? configuring.label,
              })}
            </h2>
            <div className="rea-config-notice">
              <span>i</span>
              <p>
                {configuring.id === 'ida'
                  ? t('reaSetup.configSummary.ida')
                  : configuring.assisted_setup.summary || t('reaSetup.configureHint')}
              </p>
              {configuring.assisted_setup.links[0] && (
                <a href={configuring.assisted_setup.links[0].url} target="_blank" rel="noreferrer">
                  {configuring.assisted_setup.links[0].label} ↗
                </a>
              )}
            </div>
            <div className="rea-config-fields">
              {assistedInputs.map((input) => (
                <label className="rea-config-field" key={input.id}>
                  <span>{input.label}</span>
                  {input.type === 'path' || input.type === 'directory' ? (
                    <div className="rea-config-field__path">
                      <input
                        value={configureValues[input.id] || ''}
                        placeholder={input.placeholder}
                        onChange={(event) =>
                          setConfigureValues((current) => ({ ...current, [input.id]: event.target.value }))
                        }
                        data-testid={`rea-setup-input-${configuring.id}-${input.id}`}
                      />
                      <button type="button" className="rea-config-path-button" onClick={() => void chooseInputPath(input)} aria-label={input.label}>▱</button>
                    </div>
                  ) : (
                    <textarea
                      value={configureValues[input.id] || ''}
                      placeholder={input.placeholder}
                      onChange={(event) =>
                        setConfigureValues((current) => ({ ...current, [input.id]: event.target.value }))
                      }
                      maxLength={1000}
                      data-testid={`rea-setup-input-${configuring.id}-${input.id}`}
                    />
                  )}
                  {input.type !== 'path' && input.type !== 'directory' && (
                    <small>{(configureValues[input.id] || '').length}/1000</small>
                  )}
                </label>
              ))}
            </div>
            <div className="rea-config-footer">
              <div className="rea-config-footer__guide">
                {hasSensitiveInput && <p className="rea-config-warning rea-sr-only">{t('reaSetup.secretWarning')}</p>}
                {(configuring.assisted_setup.prerequisites?.length || configuring.assisted_setup.instructions.length > 0) && (
                  <details className="rea-config-guide">
                    <summary>{t('reaSetup.configurationGuide')}</summary>
                    {configuring.assisted_setup.prerequisites?.length ? (
                      <ul>
                        {configuring.assisted_setup.prerequisites.map((item) => <li key={item}>{item}</li>)}
                      </ul>
                    ) : null}
                    <ol>
                      {configuring.assisted_setup.instructions.map((item) => <li key={item}>{item}</li>)}
                    </ol>
                    {(configuring.assisted_setup.commands ?? []).map((command, index) => (
                      <div className="rea-config-command" key={`${command.label}-${index}`}>
                        <code>{command.content}</code>
                        <button type="button" className="rea-text-action" onClick={() => void copyCommand(`${configuring.id}-${index}`, command.content)}>
                          {copiedCommand === `${configuring.id}-${index}` ? t('reaSetup.copied') : t('reaSetup.copy')}
                        </button>
                      </div>
                    ))}
                  </details>
                )}
              </div>
              <div className="rea-config-actions">
                <button type="button" className="rea-button rea-button--outline rea-button--small" onClick={() => setConfiguring(null)}>
                  {t('common.cancel')}
                </button>
                {assistedInputs.length > 0 && (
                  <button
                    type="button"
                    className="rea-button rea-button--dark rea-button--small"
                    disabled={configureDisabled}
                    onClick={() => void configure()}
                    data-testid="rea-setup-config-confirm"
                  >
                    {configureSaving ? t('reaSetup.configuring') : t('reaSetup.confirm')}
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const REA_VERSION_FALLBACK = '6.8.0';
