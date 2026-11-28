/**
 * 专家团结束后侧栏空转：App.tsx 在 session.get_metadata 时
 * 「team + 服务端 idle + 本地 busy → 强制灭灯」的回归测试。
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { loadCallback } from './loadProductionCallback.mjs';
import { useChatStore } from '../node_modules/.cache/team-sidebar-spin/stores/chatStore.mjs';
import { useSessionStore } from '../node_modules/.cache/team-sidebar-spin/stores/sessionStore.mjs';
import { isTeamAgentMode } from '../node_modules/.cache/team-sidebar-spin/features/planMode/wireMode.mjs';
import { maySnapshotRelightProcessing } from '../node_modules/.cache/team-sidebar-spin/features/interruptSettleGuard.mjs';
import {
  markCronOptimisticProcessing,
  resetCronOptimisticProcessingForTests,
  shouldKeepLocalCronProcessing,
} from '../node_modules/.cache/team-sidebar-spin/features/cron/cronProcessingGuard.mjs';

const SESSION_ID = 'web_team_sidebar_spin_idle_sync';

function createRefresh({
  mode = 'team',
  metadata = { session_id: SESSION_ID, is_processing: false, status: 'idle' },
  recoveringExecution = false,
  patches = [],
  requestImpl = null,
  cronLastRunSessionId = {},
} = {}) {
  useChatStore.getState().ensureRuntime(SESSION_ID);
  useSessionStore.getState().ensureRuntime(SESSION_ID);
  useSessionStore.getState().setMode(SESSION_ID, mode);
  useChatStore.getState().setProcessing(SESSION_ID, true);
  assert.equal(useChatStore.getState().getRuntime(SESSION_ID).foregroundIsProcessing, true);

  const chat = useChatStore.getState();
  const refresh = loadCallback('App.tsx', 'fetchSessionMetadata', {
    browserClientCapabilities: { getEpoch: () => 0 },
    organizationOwnerStateRevision: () => 0,
    isOrganizationOwnerStopped: () => false,
    setOrganizationOwnerStopped: () => {},
    useChatStore,
    useSessionStore,
    useCronStore: { getState: () => ({ lastRunSessionId: cronLastRunSessionId, jobs: [] }) },
    useWorkspaceStore: {
      getState: () => ({
        upsertSession() {},
        patchSession(id, patch) {
          patches.push({ id, patch });
        },
      }),
    },
    sessionIdRef: { current: 'other-session' },
    metadataDowngradeAllowedRef: { current: new Set() },
    webClient: {
      isRecoveringExecution: () => recoveringExecution,
    },
    maySnapshotRelightProcessing,
    shouldKeepLocalCronProcessing,
    isTeamAgentMode,
    upsertSessionMetadata() {},
    restoreSessionEquipment() {},
    restoreSessionModelSelection() {},
    parseModelSelection: () => null,
    normalizeTeamLeaderIdentity: (value) => value,
    normalizeTeamDispatchMode: () => null,
    normalizeTeamBootstrapMode: () => null,
    resolveRestoredTeamDispatchMode: () => null,
    enforceSwarmflowPlanPrecedence() {},
    restoredGitIdentity: () => undefined,
    navigate() {},
    setMissingSessionId() {},
    setProcessing: (...args) => chat.setProcessing(...args),
    setThinking: (...args) => chat.setThinking(...args),
    request: requestImpl ?? (async () => metadata),
    console: { warn() {} },
  });

  return { refresh, patches, chat };
}

function cleanup() {
  useChatStore.getState().removeRuntime(SESSION_ID);
  useSessionStore.getState().removeRuntime(SESSION_ID);
  resetCronOptimisticProcessingForTests();
}

test('team + server idle + local busy: metadata refresh clears processing lamp', async () => {
  const patches = [];
  const { refresh, chat } = createRefresh({ patches });
  try {
    const session = await refresh(SESSION_ID);
    assert.ok(session);
    assert.equal(session.is_processing, false);
    assert.equal(chat.getRuntime(SESSION_ID).foregroundIsProcessing, false);
    assert.equal(chat.getRuntime(SESSION_ID).isProcessing, false);
    assert.ok(
      patches.some((item) => item.id === SESSION_ID && item.patch?.is_processing === false),
      'workspace session.is_processing must be patched false',
    );
  } finally {
    cleanup();
  }
});

test('agent mode does not use the team forced-idle path when metadata is idle', async () => {
  const patches = [];
  const { refresh, chat } = createRefresh({ mode: 'agent', patches });
  try {
    await refresh(SESSION_ID);
    // 无 allowDowngrade 时旧守卫不会把 busy→idle；team 专用旁路也不该触发
    assert.equal(chat.getRuntime(SESSION_ID).foregroundIsProcessing, true);
    assert.equal(
      patches.some((item) => item.patch?.is_processing === false),
      false,
    );
  } finally {
    cleanup();
  }
});

test('team forced-idle is skipped while execution recovery is in progress', async () => {
  const patches = [];
  const { refresh, chat } = createRefresh({ recoveringExecution: true, patches });
  try {
    await refresh(SESSION_ID);
    assert.equal(chat.getRuntime(SESSION_ID).foregroundIsProcessing, true);
    assert.equal(
      patches.some((item) => item.patch?.is_processing === false),
      false,
    );
  } finally {
    cleanup();
  }
});

test('team forced-idle is skipped when turnRevision advances during metadata fetch', async () => {
  const patches = [];
  let releaseRequest;
  const pendingMetadata = new Promise((resolve) => {
    releaseRequest = resolve;
  });
  const { refresh, chat } = createRefresh({
    patches,
    requestImpl: async () => pendingMetadata,
  });
  try {
    const revisionBefore = chat.getRuntime(SESSION_ID).turnRevision;
    const pending = refresh(SESSION_ID);
    // 请求在途期间用户新开一轮：滞后 idle 快照不得压灭新 turn
    chat.setProcessing(SESSION_ID, true, { newTurn: true });
    assert.equal(chat.getRuntime(SESSION_ID).turnRevision, revisionBefore + 1);

    releaseRequest({ session_id: SESSION_ID, is_processing: false, status: 'idle' });
    await pending;

    assert.equal(chat.getRuntime(SESSION_ID).foregroundIsProcessing, true);
    assert.equal(
      patches.some((item) => item.patch?.is_processing === false),
      false,
    );
  } finally {
    cleanup();
  }
});

test('team forced-idle respects cron optimistic processing grace', async () => {
  const patches = [];
  markCronOptimisticProcessing(SESSION_ID);
  const { refresh, chat } = createRefresh({
    patches,
    metadata: {
      session_id: SESSION_ID,
      is_processing: false,
      status: 'idle',
      cron_id: 'job_team_cron',
    },
    cronLastRunSessionId: { job_team_cron: SESSION_ID },
  });
  try {
    await refresh(SESSION_ID);
    // cron 宽限期内 metadata 误报 false，不得灭掉「任务执行」提示
    assert.equal(chat.getRuntime(SESSION_ID).foregroundIsProcessing, true);
    assert.equal(
      patches.some((item) => item.patch?.is_processing === false),
      false,
    );
  } finally {
    cleanup();
  }
});
