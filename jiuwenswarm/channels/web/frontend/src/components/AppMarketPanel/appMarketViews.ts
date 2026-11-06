import { webRequest } from '../../services/webClient';
import { webAppViews } from './webAppViews';

export interface InstanceInfo {
  instance_id: string;
  app_id: string;
  port: number;
  state: string;
  detail: string;
  url?: string;
  web_url?: string;
  created_at: string;
  started_at?: string | null;
}

type CloseView = () => boolean | Promise<boolean>;
type View = { instanceId: string; port: number; close: CloseView };

// Windows and iframes can outlive the market panel, so their polling must too.
const views = new Map<string, View>();
let panelPoll: (() => Promise<void>) | undefined;
let timer: ReturnType<typeof setInterval> | undefined;
let requestId = 0;
let latestResponse = 0;

function syncPolling() {
  if (panelPoll || views.size) {
    timer ??= setInterval(() => {
      void (panelPoll ?? readAppMarketInstances)().catch(() => undefined);
    }, 3000);
  } else if (timer !== undefined) {
    clearInterval(timer);
    timer = undefined;
  }
}

export function watchAppMarketInstances(poll: () => Promise<void>) {
  panelPoll = poll;
  syncPolling();
  return () => {
    if (panelPoll === poll) panelPoll = undefined;
    syncPolling();
  };
}

export async function closeAppMarketView(appId: string, instanceId?: string, fallback?: CloseView) {
  const view = views.get(appId);
  if (view && instanceId !== undefined && view.instanceId !== instanceId) return true;
  views.delete(appId);
  syncPolling();
  return (view?.close ?? fallback)?.() ?? true;
}

export function trackAppMarketView(instance: InstanceInfo, close: CloseView) {
  const { app_id: appId, instance_id: instanceId, port } = instance;
  if (views.get(appId)?.instanceId === instanceId) return;
  for (const [id, view] of views) {
    if (view.instanceId !== instanceId && (id === appId || view.port === port)) {
      void closeAppMarketView(id, view.instanceId).catch(() => undefined);
    }
  }
  const view = { instanceId, port, close };
  views.set(appId, view);
  syncPolling();
  return () => {
    if (views.get(appId) === view) {
      views.delete(appId);
      syncPolling();
    }
  };
}

export function hasAppMarketView(appId: string, instanceId: string) {
  return views.get(appId)?.instanceId === instanceId;
}

export async function openAppMarketView(instance: InstanceInfo, name: string, embedded = false) {
  if (!instance.url) return false;
  const desktop = window.jiuwenDesktop;
  if (desktop?.isElectron && (!desktop.openMarketApp || (embedded && !desktop.setMarketAppViewport))) return false;
  const untrack = trackAppMarketView(instance, () => desktop?.isElectron
    ? desktop.closeMarketApp?.(instance.app_id) ?? false : webAppViews.close(instance.app_id));
  try {
    if (desktop?.isElectron) {
      const opened = embedded
        ? await desktop.openMarketApp!(instance.app_id, instance.url, name, true)
        : await desktop.openMarketApp!(instance.app_id, instance.url, name);
      if (!opened) { untrack?.(); return false; }
    } else {
      webAppViews.open(instance.app_id, instance.web_url || instance.url, name);
    }
    return true;
  } catch (error) { untrack?.(); throw error; }
}

export async function readAppMarketInstances() {
  const snapshot = [...views];
  const id = ++requestId;
  const data = await webRequest<{ instances?: InstanceInfo[] }>('app_market.instances', {});
  if (id < latestResponse) return null;
  latestResponse = id;
  const instances = data.instances || [];
  if (data.instances) {
    const live = new Set(instances.filter(item => item.state !== 'stopped').map(item => item.instance_id));
    for (const [appId, view] of snapshot) {
      if (views.get(appId) === view && !live.has(view.instanceId)) {
        void closeAppMarketView(appId, view.instanceId).catch(() => undefined);
      }
    }
  }
  return instances;
}
