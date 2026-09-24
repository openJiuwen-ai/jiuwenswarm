/**
 * PersonalContextImLearningPanel — 「IM 学习」子页（数字分身学习配置面）。
 *
 * 三区块（对齐迁移方案 §4.5）：
 * 1. 状态卡：host_active/running/enabled 徽标 + 总闸联动提示 + fetch 指标 +
 *    backfill 每目标三态（pending/truncated/complete）+ index 推进 + last_error +
 *    「立即学习一轮」。运行中 5s 轮询，未运行降频 30s（方案 D8）。
 * 2. 学习范围：渠道下拉 → im.hosting.discover 会话选择器（已在白名单的打
 *    「学习中」标记，前端按三元组 diff）+ 批量粘贴 external_id +
 *    时间范围 preset（近 30/90/180/365 天/不限 ↔ since_ms，非 preset 回显「自定义」）。
 * 3. 周期设置：fetch_interval_seconds / fetch_top_n；distill 只读降级展示（装配缺失）。
 *
 * 交互：本地草稿 + imLearningConfig 语义签名脏检查（title 不参与）→
 * 「保存学习配置」走 runtime.patch_config {im_learning} 整节提交；
 * 移除会话仅停止新增采集，已学语料保留（文案如实传达）。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, PlayCircle, Plus, X } from 'lucide-react';
import { Switch } from '../Switch';
import { usePersonalContextStore } from '../../stores';
import {
  type ImLearningBackfill,
  type ImLearningBackfillRow,
  type ImLearningChannelId,
  type ImLearningConfig,
  type ImLearningDiscoverItem,
  type ImLearningTarget,
  type ImLearningTargetKind,
  IM_LEARNING_CHANNELS,
  pcApi,
  webErrorCode,
} from '../../services/personalContextApi';
import { PersonalContextImAssociateDialog } from './ImAssociateDialog';
import {
  SINCE_PRESET_DAYS,
  type SincePresetDays,
  discoverItemToTarget,
  isImLearningDirty,
  markDiscoveredItems,
  mergeTargets,
  parseBulkExternalIds,
  presetDaysToSinceMs,
  sinceMsToPreset,
  targetKey,
  validateImLearningDraft,
} from './imLearningConfig';
import './ImLearningPanel.css';

const POLL_ACTIVE_MS = 5000;
const POLL_IDLE_MS = 30_000;

type PanelNotice =
  | { kind: 'success'; key: string }
  | { kind: 'error'; message: string };

interface PersonalContextImLearningPanelProps {
  isConnected: boolean;
  isActive: boolean;
  onBackToGraph: () => void;
}

function formatMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return '';
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return '';
  const pad = (part: number) => String(part).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function backfillRowKey(row: ImLearningBackfillRow | ImLearningTarget): string {
  return `${row.channel_id}:${row.kind}:${row.external_id}`;
}

type BackfillState = 'pending' | 'truncated' | 'complete';

function backfillStateOf(
  target: ImLearningTarget,
  backfill: ImLearningBackfill | undefined,
): BackfillState | null {
  if (!backfill) return null;
  const key = backfillRowKey(target);
  if ((backfill.truncated ?? []).some((row) => backfillRowKey(row) === key)) return 'truncated';
  if (backfill.pending.some((row) => backfillRowKey(row) === key)) return 'pending';
  return 'complete';
}

function discoverItemKey(item: ImLearningDiscoverItem): string {
  return targetKey({
    channel_id: item.channel_id,
    kind: item.target_kind,
    external_id: item.external_id,
  });
}

export function PersonalContextImLearningPanel({
  isConnected,
  isActive,
  onBackToGraph,
}: PersonalContextImLearningPanelProps) {
  const { t } = useTranslation();
  const {
    config,
    imLearningStatus,
    pendingWrites,
    loadImLearningStatus,
    saveImLearning,
    setInfoTab,
  } = usePersonalContextStore();

  const savedIm = config.im_learning;

  // 本地草稿：仅在已保存配置的语义签名变化时（保存成功/远端刷新）重置，不打断编辑。
  const [draft, setDraft] = useState<ImLearningConfig>(savedIm);
  const savedSignature = useMemo(() => JSON.stringify(savedIm), [savedIm]);
  const syncedSignatureRef = useRef(savedSignature);
  useEffect(() => {
    if (syncedSignatureRef.current === savedSignature) return;
    syncedSignatureRef.current = savedSignature;
    setDraft(savedIm);
  }, [savedSignature, savedIm]);

  const [notice, setNotice] = useState<PanelNotice | null>(null);

  // 会话发现（im.hosting.discover，与托管 UI 数据同源）
  const [discoverChannel, setDiscoverChannel] = useState<ImLearningChannelId>('feishu');
  const [discoverItems, setDiscoverItems] = useState<ImLearningDiscoverItem[] | null>(null);
  const [discoverLoading, setDiscoverLoading] = useState(false);
  const [checkedKeys, setCheckedKeys] = useState<Set<string>>(new Set());

  // 批量粘贴 external_id
  const [bulkOpen, setBulkOpen] = useState(false);
  const [bulkKind, setBulkKind] = useState<ImLearningTargetKind>('group');
  const [bulkText, setBulkText] = useState('');

  // 即时关联：渠道未登录（IM_NOT_LOGGED_IN）时弹统一关联浮层
  const [associateChannel, setAssociateChannel] = useState<ImLearningChannelId | null>(null);

  const running = imLearningStatus?.running ?? false;

  // 阶段状态轮询：运行中 5s，未运行降频 30s。
  useEffect(() => {
    if (!isConnected || !isActive) return;
    const load = () => void loadImLearningStatus().catch(() => {});
    load();
    const interval = window.setInterval(load, running ? POLL_ACTIVE_MS : POLL_IDLE_MS);
    return () => window.clearInterval(interval);
  }, [isConnected, isActive, running, loadImLearningStatus]);

  // 成功提示 5s 自动消失
  useEffect(() => {
    if (notice?.kind !== 'success') return;
    const timer = window.setTimeout(() => setNotice(null), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const markedDiscoverItems = useMemo(
    () => (discoverItems ? markDiscoveredItems(discoverItems, draft.targets) : []),
    [discoverItems, draft.targets],
  );

  const dirty = isImLearningDirty(savedIm, draft);
  const validationError = validateImLearningDraft(draft);
  const saving = !!pendingWrites.im_learning;
  const sincePreset = sinceMsToPreset(draft.since_ms);

  const handleSave = () => {
    setNotice(null);
    const errorKey = validateImLearningDraft(draft);
    if (errorKey) {
      setNotice({ kind: 'error', message: t(`personalContext.imLearning.errors.${errorKey}`) });
      return;
    }
    void saveImLearning(draft)
      .then(() => setNotice({ kind: 'success', key: 'personalContext.imLearning.saved' }))
      .catch((e: unknown) =>
        setNotice({ kind: 'error', message: e instanceof Error ? e.message : String(e) }),
      );
  };

  const handleRunNow = () => {
    setNotice(null);
    void pcApi
      .runImLearningNow()
      .then(({ triggered }) => {
        if (triggered) {
          setNotice({ kind: 'success', key: 'personalContext.imLearning.runSubmitted' });
        } else {
          setNotice({ kind: 'error', message: t('personalContext.imLearning.runNowInactive') });
        }
        void loadImLearningStatus().catch(() => {});
      })
      .catch((e: unknown) =>
        setNotice({ kind: 'error', message: e instanceof Error ? e.message : String(e) }),
      );
  };

  const handleDiscover = () => {
    setNotice(null);
    setDiscoverLoading(true);
    pcApi
      .discoverImConversations(discoverChannel)
      .then((response) => {
        setDiscoverItems(response.conversations ?? []);
        setCheckedKeys(new Set());
      })
      .catch((e: unknown) => {
        if (webErrorCode(e) === 'IM_NOT_LOGGED_IN') {
          // 即时关联：先完成渠道关联，成功后由浮层回调重放本动作。
          setAssociateChannel(discoverChannel);
          return;
        }
        setNotice({ kind: 'error', message: e instanceof Error ? e.message : String(e) });
      })
      .finally(() => setDiscoverLoading(false));
  };

  const toggleChecked = (key: string) => {
    setCheckedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleAddChecked = () => {
    const additions = markedDiscoverItems
      .filter((item) => checkedKeys.has(discoverItemKey(item)))
      .map(discoverItemToTarget);
    if (!additions.length) return;
    setDraft((prev) => ({ ...prev, targets: mergeTargets(prev.targets, additions) }));
    setCheckedKeys(new Set());
  };

  const handleRemoveTarget = (target: ImLearningTarget) => {
    if (!window.confirm(t('personalContext.imLearning.removeConfirm'))) return;
    const key = targetKey(target);
    setDraft((prev) => ({ ...prev, targets: prev.targets.filter((x) => targetKey(x) !== key) }));
  };

  const handleBulkAdd = () => {
    setNotice(null);
    const { targets, invalidLines } = parseBulkExternalIds(bulkText, discoverChannel, bulkKind);
    if (invalidLines.length) {
      setNotice({
        kind: 'error',
        message: t('personalContext.imLearning.bulkInvalidLines', {
          lines: invalidLines.join(', '),
        }),
      });
      return;
    }
    if (!targets.length) {
      setNotice({ kind: 'error', message: t('personalContext.imLearning.bulkEmpty') });
      return;
    }
    setDraft((prev) => ({ ...prev, targets: mergeTargets(prev.targets, targets) }));
    setBulkText('');
    setBulkOpen(false);
  };

  const applySincePreset = (days: SincePresetDays | null) => {
    setDraft((prev) => ({
      ...prev,
      since_ms: days == null ? null : presetDaysToSinceMs(days),
    }));
  };

  const sinceOptions: Array<{ value: SincePresetDays | null; label: string }> = [
    { value: null, label: t('personalContext.imLearning.timeRangeAll') },
    ...SINCE_PRESET_DAYS.map((days) => ({
      value: days as SincePresetDays | null,
      label: t('personalContext.imLearning.timeRangeDays', { n: days }),
    })),
  ];

  const status = imLearningStatus;
  const backfill = status?.backfill;
  const gateBlocked = draft.enabled && !config.collection_enabled;

  return (
    <div className="pc-iml" data-testid="personal-context-im-learning">
      {notice && (
        <div
          className={`pc-iml__error${notice.kind === 'success' ? ' pc-iml__notice--success' : ' pc-iml__error--dismissible'}`}
          role={notice.kind === 'success' ? 'status' : 'alert'}
        >
          <span>{notice.kind === 'success' ? t(notice.key) : notice.message}</span>
          {notice.kind === 'error' && (
            <button
              type="button"
              className="pc-iml__error-close"
              aria-label={t('common.close')}
              onClick={() => setNotice(null)}
            >
              <X size={14} />
            </button>
          )}
        </div>
      )}

      <div className="pc-iml__head">
        <button type="button" className="pc-iml__back" onClick={onBackToGraph}>
          <span className="pc-iml__back-icon">&lt;</span>
          <span>{t('personalContext.imLearning.backToGraph')}</span>
        </button>
        <div className="pc-iml__head-row">
          <div className="pc-iml__head-text">
            <h3 className="pc-iml__head-title">{t('personalContext.imLearning.title')}</h3>
            <p className="pc-iml__head-subtitle">{t('personalContext.imLearning.subtitle')}</p>
          </div>
        </div>
      </div>

      {/* ── 区块 1：状态卡 ── */}
      <section className="pc-iml__card">
        <div className="pc-iml__card-head">
          <h4 className="pc-iml__card-title">{t('personalContext.imLearning.statusTitle')}</h4>
          <button
            type="button"
            className="pc-iml__run-now"
            onClick={handleRunNow}
            disabled={!isConnected}
          >
            <PlayCircle size={16} />
            {t('personalContext.imLearning.runNow')}
          </button>
        </div>

        {status == null ? (
          <div className="pc-iml__status-empty">{t('personalContext.imLearning.statusUnavailable')}</div>
        ) : (
          <>
            <div className="pc-iml__badges">
              <span className={`pc-iml__badge${status.host_active ? ' is-ok' : ' is-bad'}`}>
                {status.host_active
                  ? t('personalContext.imLearning.badgeHostActive')
                  : t('personalContext.imLearning.badgeHostInactive')}
              </span>
              <span className={`pc-iml__badge${status.running ? ' is-ok' : ''}`}>
                {status.running
                  ? t('personalContext.imLearning.badgeRunning')
                  : t('personalContext.imLearning.badgeStopped')}
              </span>
              <span className={`pc-iml__badge${status.enabled ? ' is-ok' : ''}`}>
                {status.enabled
                  ? t('personalContext.imLearning.badgeEnabled')
                  : t('personalContext.imLearning.badgeDisabled')}
              </span>
            </div>

            {gateBlocked && (
              <div className="pc-iml__gate" role="alert">
                <span>{t('personalContext.imLearning.gateHint')}</span>
                <button type="button" onClick={() => setInfoTab('settings')}>
                  {t('personalContext.imLearning.gateAction')}
                </button>
              </div>
            )}

            <div className="pc-iml__metrics">
              <div className="pc-iml__metric">
                <span className="pc-iml__metric-label">{t('personalContext.imLearning.metricTargets')}</span>
                <span className="pc-iml__metric-value">{status.targets ?? draft.targets.length}</span>
              </div>
              <div className="pc-iml__metric">
                <span className="pc-iml__metric-label">{t('personalContext.imLearning.metricLastCycle')}</span>
                <span className="pc-iml__metric-value">
                  {formatMs(status.last_cycle_at_ms) || t('personalContext.imLearning.metricNever')}
                </span>
              </div>
              <div className="pc-iml__metric">
                <span className="pc-iml__metric-label">{t('personalContext.imLearning.metricPersisted')}</span>
                <span className="pc-iml__metric-value">{status.last_cycle_persisted ?? 0}</span>
              </div>
              <div className="pc-iml__metric">
                <span className="pc-iml__metric-label">{t('personalContext.imLearning.metricErrors')}</span>
                <span className={`pc-iml__metric-value${status.last_cycle_errors ? ' is-bad' : ''}`}>
                  {status.last_cycle_errors ?? 0}
                </span>
              </div>
              <div className="pc-iml__metric">
                <span className="pc-iml__metric-label">{t('personalContext.imLearning.metricIndexed')}</span>
                <span className="pc-iml__metric-value">
                  {formatMs(status.last_indexed_at_ms) || t('personalContext.imLearning.metricNever')}
                </span>
              </div>
            </div>

            {backfill && savedIm.targets.length > 0 && (
              <div className="pc-iml__backfill">
                <div className="pc-iml__backfill-head">
                  <span>{t('personalContext.imLearning.backfillTitle')}</span>
                  {backfill.ready && (
                    <span className="pc-iml__backfill-ready">
                      {t('personalContext.imLearning.backfillReady')}
                    </span>
                  )}
                </div>
                <div className="pc-iml__backfill-list">
                  {savedIm.targets.map((target) => {
                    const state = backfillStateOf(target, backfill);
                    return (
                      <div className="pc-iml__backfill-row" key={targetKey(target)}>
                        <span className="pc-iml__row-name" title={target.external_id}>
                          {target.title || target.external_id}
                        </span>
                        <span className="pc-iml__row-sub">
                          {t(`personalContext.imLearning.channel.${target.channel_id}`)} ·{' '}
                          {t(`personalContext.imLearning.kind.${target.kind}`)}
                        </span>
                        {state === 'pending' && (
                          <span className="pc-iml__state pc-iml__state--pending">
                            {t('personalContext.imLearning.backfillPending')}
                          </span>
                        )}
                        {state === 'truncated' && (
                          <span
                            className="pc-iml__state pc-iml__state--truncated"
                            title={t('personalContext.imLearning.backfillTruncatedHint')}
                          >
                            {t('personalContext.imLearning.backfillTruncated')}
                          </span>
                        )}
                        {state === 'complete' && (
                          <span className="pc-iml__state pc-iml__state--complete">
                            {t('personalContext.imLearning.backfillComplete')}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {(status.active_runs?.length ?? 0) > 0 && (
              <div className="pc-iml__runs">
                <span className="pc-iml__runs-label">
                  {t('personalContext.imLearning.activeRunsTitle')}
                </span>
                {(status.active_runs ?? []).map((run) => (
                  <span className="pc-iml__run-chip" key={run.id} title={run.source_key ?? run.id}>
                    {run.stage === 'fetch'
                      ? t('personalContext.imLearning.activeRunFetch')
                      : t('personalContext.imLearning.activeRunIndex')}
                  </span>
                ))}
              </div>
            )}

            {status.last_error && (
              <div className="pc-iml__last-error" role="alert">
                <span>{t('personalContext.imLearning.lastErrorTitle')}：{status.last_error}</span>
              </div>
            )}
          </>
        )}
      </section>

      {/* ── 区块 2：学习范围（白名单） ── */}
      <section className="pc-iml__card">
        <div className="pc-iml__card-head">
          <div className="pc-iml__card-head-text">
            <h4 className="pc-iml__card-title">{t('personalContext.imLearning.scopeTitle')}</h4>
            <p className="pc-iml__card-sub">{t('personalContext.imLearning.scopeSubtitle')}</p>
          </div>
          <div className="pc-iml__enable">
            <span className="pc-iml__enable-label">{t('personalContext.imLearning.enableLabel')}</span>
            <Switch
              checked={draft.enabled}
              onChange={(enabled) => setDraft((prev) => ({ ...prev, enabled }))}
              disabled={saving}
              title={t('personalContext.imLearning.enableHint')}
            />
          </div>
        </div>

        <div className="pc-iml__discover">
          <div className="pc-iml__discover-controls">
            <label className="pc-iml__label" htmlFor="pc-iml-channel">
              {t('personalContext.imLearning.discoverChannelLabel')}
            </label>
            <select
              id="pc-iml-channel"
              className="pc-iml__select"
              value={discoverChannel}
              onChange={(e) => {
                setDiscoverChannel(e.target.value as ImLearningChannelId);
                setDiscoverItems(null);
                setCheckedKeys(new Set());
              }}
            >
              {IM_LEARNING_CHANNELS.map((id) => (
                <option key={id} value={id}>
                  {t(`personalContext.imLearning.channel.${id}`)}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="pc-iml__btn"
              onClick={handleDiscover}
              disabled={!isConnected || discoverLoading}
            >
              {discoverLoading ? <Loader2 className="spin" size={14} /> : null}
              {t('personalContext.imLearning.discoverButton')}
            </button>
            <button
              type="button"
              className="pc-iml__btn"
              onClick={() => setBulkOpen((open) => !open)}
              disabled={!isConnected}
            >
              {t('personalContext.imLearning.bulkButton')}
            </button>
          </div>

          {bulkOpen && (
            <div className="pc-iml__bulk">
              <div className="pc-iml__bulk-controls">
                <label className="pc-iml__label" htmlFor="pc-iml-bulk-kind">
                  {t('personalContext.imLearning.bulkKindLabel')}
                </label>
                <select
                  id="pc-iml-bulk-kind"
                  className="pc-iml__select"
                  value={bulkKind}
                  onChange={(e) => setBulkKind(e.target.value as ImLearningTargetKind)}
                >
                  <option value="group">{t('personalContext.imLearning.kind.group')}</option>
                  <option value="user">{t('personalContext.imLearning.kind.user')}</option>
                </select>
              </div>
              <textarea
                className="pc-iml__bulk-text"
                value={bulkText}
                onChange={(e) => setBulkText(e.target.value)}
                placeholder={t('personalContext.imLearning.bulkPlaceholder')}
                rows={4}
              />
              <div className="pc-iml__bulk-actions">
                <button type="button" className="pc-iml__btn" onClick={handleBulkAdd}>
                  <Plus size={14} />
                  {t('personalContext.imLearning.bulkAdd')}
                </button>
                <button
                  type="button"
                  className="pc-iml__btn pc-iml__btn--ghost"
                  onClick={() => {
                    setBulkOpen(false);
                    setBulkText('');
                  }}
                >
                  {t('personalContext.imLearning.bulkCancel')}
                </button>
              </div>
            </div>
          )}

          {discoverItems != null && (
            <div className="pc-iml__discover-list">
              {markedDiscoverItems.length === 0 ? (
                <div className="pc-iml__discover-empty">
                  {t('personalContext.imLearning.discoverEmpty')}
                </div>
              ) : (
                <>
                  {markedDiscoverItems.map((item) => {
                    const key = discoverItemKey(item);
                    const checked = checkedKeys.has(key);
                    return (
                      <label
                        key={key}
                        className={`pc-iml__discover-item${item.learning ? ' is-learning' : ''}`}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={item.learning}
                          onChange={() => toggleChecked(key)}
                        />
                        <span className="pc-iml__tag">{t(`personalContext.imLearning.kind.${item.target_kind}`)}</span>
                        <span className="pc-iml__row-name" title={item.external_id}>
                          {item.title || item.external_id}
                        </span>
                        <span className="pc-iml__row-sub">{item.external_id}</span>
                        {item.learning && (
                          <span className="pc-iml__learning-mark">
                            {t('personalContext.imLearning.learningMark')}
                          </span>
                        )}
                      </label>
                    );
                  })}
                  <button
                    type="button"
                    className="pc-iml__btn pc-iml__btn--accent"
                    onClick={handleAddChecked}
                    disabled={checkedKeys.size === 0}
                  >
                    <Plus size={14} />
                    {t('personalContext.imLearning.addSelected', { n: checkedKeys.size })}
                  </button>
                </>
              )}
            </div>
          )}
        </div>

        <div className="pc-iml__targets">
          <div className="pc-iml__targets-head">
            <span className="pc-iml__targets-title">
              {t('personalContext.imLearning.targetsTitle')}
            </span>
            <span className="pc-iml__targets-count">
              {t('personalContext.imLearning.selectedCount', { n: draft.targets.length })}
            </span>
          </div>
          {draft.targets.length === 0 ? (
            <div className="pc-iml__targets-empty">
              {t('personalContext.imLearning.targetsEmpty')}
            </div>
          ) : (
            <div className="pc-iml__targets-list">
              {draft.targets.map((target) => (
                <div className="pc-iml__target-row" key={targetKey(target)}>
                  <span className="pc-iml__tag">{t(`personalContext.imLearning.kind.${target.kind}`)}</span>
                  <span className="pc-iml__row-name" title={target.external_id}>
                    {target.title || target.external_id}
                  </span>
                  <span className="pc-iml__row-sub">
                    {t(`personalContext.imLearning.channel.${target.channel_id}`)} · {target.external_id}
                  </span>
                  <button
                    type="button"
                    className="pc-iml__remove"
                    onClick={() => handleRemoveTarget(target)}
                    disabled={saving}
                  >
                    {t('personalContext.imLearning.remove')}
                  </button>
                </div>
              ))}
            </div>
          )}
          <p className="pc-iml__targets-note">{t('personalContext.imLearning.targetsNote')}</p>
        </div>

        <div className="pc-iml__since">
          <span className="pc-iml__label">{t('personalContext.imLearning.timeRangeLabel')}</span>
          <div className="pc-iml__since-options">
            {sinceOptions.map((option) => (
              <button
                key={String(option.value)}
                type="button"
                className={`pc-iml__since-btn${sincePreset === option.value ? ' is-active' : ''}`}
                onClick={() => applySincePreset(option.value)}
                disabled={saving}
              >
                {option.label}
              </button>
            ))}
            {sincePreset === 'custom' && (
              <span
                className="pc-iml__since-custom"
                title={t('personalContext.imLearning.timeRangeCustomHint')}
              >
                {t('personalContext.imLearning.timeRangeCustom')}
              </span>
            )}
          </div>
        </div>
      </section>

      {/* ── 区块 3：周期设置 ── */}
      <section className="pc-iml__card">
        <div className="pc-iml__card-head">
          <h4 className="pc-iml__card-title">{t('personalContext.imLearning.cycleTitle')}</h4>
        </div>
        <div className="pc-iml__cycle">
          <div className="pc-iml__field">
            <label className="pc-iml__label" htmlFor="pc-iml-interval">
              {t('personalContext.imLearning.intervalLabel')}
            </label>
            <input
              id="pc-iml-interval"
              className="pc-iml__input"
              type="number"
              min={1}
              step={60}
              value={draft.fetch_interval_seconds}
              onChange={(e) =>
                setDraft((prev) => ({
                  ...prev,
                  fetch_interval_seconds: Number(e.target.value),
                }))
              }
              disabled={saving}
            />
            <p className="pc-iml__hint">{t('personalContext.imLearning.intervalHint')}</p>
          </div>
          <div className="pc-iml__field">
            <label className="pc-iml__label" htmlFor="pc-iml-topn">
              {t('personalContext.imLearning.topNLabel')}
            </label>
            <input
              id="pc-iml-topn"
              className="pc-iml__input"
              type="number"
              min={1}
              max={200}
              step={10}
              value={draft.fetch_top_n}
              onChange={(e) =>
                setDraft((prev) => ({ ...prev, fetch_top_n: Number(e.target.value) }))
              }
              disabled={saving}
            />
            <p className="pc-iml__hint">{t('personalContext.imLearning.topNHint')}</p>
          </div>
        </div>
        <div className="pc-iml__distill">
          <span className="pc-iml__distill-label">{t('personalContext.imLearning.distillLabel')}</span>
          <span className="pc-iml__distill-value">
            {t('personalContext.imLearning.distillDisabled')}
          </span>
        </div>
      </section>

      <div className="pc-iml__save-bar">
        {validationError && (
          <span className="pc-iml__save-error">
            {t(`personalContext.imLearning.errors.${validationError}`)}
          </span>
        )}
        {!validationError && dirty && (
          <span className="pc-iml__save-hint">{t('personalContext.imLearning.dirtyHint')}</span>
        )}
        <button
          type="button"
          className="pc-iml__save"
          onClick={handleSave}
          disabled={!isConnected || !dirty || saving || !!validationError}
        >
          {saving ? (
            <>
              <Loader2 className="spin" size={14} />
              {t('personalContext.imLearning.saving')}
            </>
          ) : (
            t('personalContext.imLearning.save')
          )}
        </button>
      </div>

      <PersonalContextImAssociateDialog
        open={associateChannel !== null}
        channelId={associateChannel ?? discoverChannel}
        onClose={() => setAssociateChannel(null)}
        onSuccess={() => {
          setAssociateChannel(null);
          // 关联成功：自动重放「从最近会话选择」。
          handleDiscover();
          setNotice({ kind: 'success', key: 'personalContext.imLearning.associateSuccess' });
        }}
      />
    </div>
  );
}
