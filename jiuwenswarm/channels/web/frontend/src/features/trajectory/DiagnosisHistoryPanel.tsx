// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * 诊断历史面板：独立导航项，占满内容区。
 * 左栏历史列表 + 右栏全宽渲染区（空态 / 新建诊断 / 报告流式渲染）。
 * 替代旧 DiagnosisPanel 的 460px 浮层形态。
 */

import { useCallback, useEffect, useRef, useState, type HTMLAttributes } from 'react';
import ReactMarkdown from 'react-markdown';
import {
  getDiagnosisReport,
  listDiagnosisReports,
  type DiagnosisMode,
  type DiagnosisReportSummary,
} from './diagnosisClient';
import {
  getLive,
  subscribeLive,
  clearLive,
  type LiveDiagnosis,
} from './diagnosisLive';
import { startDiagnosis, cancelDiagnosis, cancelAllDiagnoses } from './diagnosisRunner';
import css from './DiagnosisHistoryPanel.module.css';

type View = 'empty' | 'new' | 'report';
type Status = 'idle' | 'running' | 'done' | 'error';
type DiagMode = 'online' | 'offline';

// webkitdirectory / directory 非标准属性，用类型断言兼容不同 @types/react 版本
const DIR_ATTRS = { webkitdirectory: '', directory: '' } as unknown as HTMLAttributes<HTMLInputElement>;

// 离线诊断会话 id / 模式持久化键：切换页面（组件卸载再挂载）后恢复，
// 否则离线的诊断历史会因 id 丢失而不显示（在线模式靠 currentSessionId 自动重载）。
const OFFLINE_SESSION_KEY = 'jiuwenswarm.diagnosis.offlineSessionId';
const DIAGNOSIS_MODE_KEY = 'jiuwenswarm.diagnosis.mode';

function readStoredOfflineId(): string {
  try {
    return localStorage.getItem(OFFLINE_SESSION_KEY) || '';
  } catch {
    return '';
  }
}

function readStoredMode(): DiagMode {
  try {
    const m = localStorage.getItem(DIAGNOSIS_MODE_KEY);
    return m === 'online' || m === 'offline' ? m : 'online';
  } catch {
    return 'online';
  }
}

export interface DiagnosisHistoryPanelProps {
  /** WS 请求函数（用于列会话，选 session 用）。 */
  request: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>;
  /** 当前会话 id（默认选中）。 */
  currentSessionId: string;
}

interface SessionOption {
  session_id: string;
  title?: string;
}

export function DiagnosisHistoryPanel({ request, currentSessionId }: DiagnosisHistoryPanelProps) {
  const [sessions, setSessions] = useState<SessionOption[]>([]);
  const [sessionId, setSessionId] = useState(currentSessionId);
  const [reports, setReports] = useState<DiagnosisReportSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [reportText, setReportText] = useState('');
  const [view, setView] = useState<View>('empty');
  const [status, setStatus] = useState<Status>('idle');
  const [stage, setStage] = useState('');
  const [errorMsg, setErrorMsg] = useState('');
  const [userNote, setUserNote] = useState('');
  const [mode, setMode] = useState<DiagnosisMode>('error');
  const [logFiles, setLogFiles] = useState<File[]>([]);
  const [diagMode, setDiagMode] = useState<DiagMode>(() => readStoredMode());
  const [pickerOpen, setPickerOpen] = useState(false);
  const [offlineSessionId, setOfflineSessionId] = useState<string>(() => readStoredOfflineId());
  const [loadingList, setLoadingList] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  // 进行中诊断的跨挂载存活镜像（来自 diagnosisLive 仓库），用于列表/右栏渲染
  const [live, setLive] = useState<LiveDiagnosis | null>(null);
  // 当前右栏展示视图的实时镜像，供流式回调判断是否覆盖（看历史报告时不打断）
  const displayViewRef = useRef<View>('empty');

  // 计时器：live 处于 running 时每秒刷新 elapsed（由 live.startedAt 推算），
  // 因此跨界面切换（组件卸载再挂载）后仍能从 startedAt 连续计时，不会归零。
  const isLiveRunning = live?.status === 'running';
  const liveStartedAt = live?.startedAt;
  useEffect(() => {
    if (!isLiveRunning || !liveStartedAt) return;
    const id = setInterval(() => {
      setElapsed(Math.floor((Date.now() - liveStartedAt) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [isLiveRunning, liveStartedAt]);

  // 右栏视图实时镜像，供流式回调判断「看历史报告时不被后台流覆盖」
  useEffect(() => {
    displayViewRef.current = view;
  }, [view]);

  // 拉取某 session 的诊断历史（声明于 applyLive 之前，供其 done 分支调用刷新列表）
  const refreshReports = useCallback(async (sid: string) => {
    setLoadingList(true);
    try {
      const list = await listDiagnosisReports(sid);
      setReports(list);
    } catch {
      setReports([]);
    } finally {
      setLoadingList(false);
    }
  }, []);

  // 把 live 仓库里的状态镜像到本地渲染状态；仅处理 running / error / done，
  // done 时落盘（刷新历史列表 + 清仓库），不自动打开某条报告（用户要求点哪条看哪条）。
  const applyLive = useCallback((l: LiveDiagnosis) => {
    setLive(l);
    if (l.status === 'running') {
      setStatus('running');
      setStage(l.stage);
      // 用户在看历史报告时，不打断其视图（仅后台继续累计，点「诊断中」可切回）
      if (displayViewRef.current !== 'report') {
        setView('new');
        setReportText(l.accumulated);
      }
    } else if (l.status === 'error') {
      setStatus('error');
      setErrorMsg(l.error || '诊断失败');
    } else if (l.status === 'done') {
      setStatus('done');
      if (l.startedAt) setElapsed(Math.floor((Date.now() - l.startedAt) / 1000));
      refreshReports(l.sessionId);
      clearLive(l.sessionId);
      setLive(null);
    }
  }, [refreshReports]);

  const fmtElapsed = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return m > 0 ? `${m}分${sec.toString().padStart(2, '0')}秒` : `${sec}秒`;
  };

  // 列会话（供下拉选择诊断目标 session）。limit 200：后端默认只回 20 条，
  // 会话多时选不到目标。
  useEffect(() => {
    let cancelled = false;
    request<{ sessions?: SessionOption[] }>('session.list', { limit: 200 })
      .then((res) => {
        if (cancelled) return;
        const list = res.sessions ?? [];
        setSessions(list.length ? list : [{ session_id: currentSessionId }]);
        if (!sessionId) setSessionId(currentSessionId);
      })
      .catch(() => {
        if (!cancelled) setSessions([{ session_id: currentSessionId }]);
      });
    return () => { cancelled = true; };
  }, [currentSessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    // 切换会话：只刷新该会话历史；不中止后台诊断流（之前 abort 会误杀正在跑的诊断）
    if (sessionId) refreshReports(sessionId);
    setReportText('');
    setSelectedId(null);
    setView('empty');
    setStatus('idle');
  }, [sessionId, refreshReports]);

  // 离线模式手输 session id：防抖刷新该 id 的历史报告（否则离线报告重进面板后不可达）
  useEffect(() => {
    const sid = offlineSessionId.trim();
    if (!sid) return;
    const timer = setTimeout(() => { refreshReports(sid); }, 500);
    return () => { clearTimeout(timer); };
  }, [offlineSessionId, refreshReports]);

  // 离线会话 id 持久化：切换页面（组件卸载再挂载）后仍可恢复离线历史
  useEffect(() => {
    try {
      if (offlineSessionId.trim()) localStorage.setItem(OFFLINE_SESSION_KEY, offlineSessionId.trim());
      else localStorage.removeItem(OFFLINE_SESSION_KEY);
    } catch {
      /* storage 不可用时静默忽略 */
    }
  }, [offlineSessionId]);

  // 诊断模式持久化：重进面板时恢复上次在线/离线状态
  useEffect(() => {
    try {
      localStorage.setItem(DIAGNOSIS_MODE_KEY, diagMode);
    } catch {
      /* storage 不可用时静默忽略 */
    }
  }, [diagMode]);

  // 选中历史报告 → 读全文
  const handleSelect = useCallback(async (summary: DiagnosisReportSummary) => {
    // 注：选中历史报告不再中止后台诊断流（之前 abort 会误杀正在跑的诊断）。
    // 仅切换右栏视图；后台流继续累计，看报告时也不被流式回调覆盖。
    setSelectedId(summary.report_id);
    setView('report');
    setStatus('idle');
    setErrorMsg('');
    setStage('');
    displayViewRef.current = 'report';
    try {
      const detail = await getDiagnosisReport(summary.session_id, summary.report_id);
      setReportText(detail.report_text);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : String(err));
      setStatus('error');
    }
  }, []);

  // 离线保活（列表层）：挂载 / 模式切换 / 历史刷新后，确保离线会话的历史已加载，
  // 使切换页面/刷新回来左侧列表仍在。不自动打开某条报告（用户要求点哪条看哪条）。
  // - reports 为空或仍属别的会话（如挂载瞬间先灌入的在线报告）：先拉一次离线历史；
  //   失败时 refreshReports 会 setReports([])，与空数组相等不会触发重复渲染，至多补一次请求。
  useEffect(() => {
    if (diagMode !== 'offline') return;
    const sid = offlineSessionId.trim();
    if (!sid) return;
    if (reports.length === 0 || reports[0]?.session_id !== sid) {
      refreshReports(sid);
    }
  }, [reports, diagMode, offlineSessionId, refreshReports]);

  // 进行中诊断保活（流式层）：挂载/会话切换时，若后台仍有该会话的 running 诊断
  // （即上一次挂载启动后切走页面，流在后台继续跑），订阅回来到本地，继续渲染流式输出。
  // 组件卸载只取消订阅，不中断后台流；订阅到的 live 仅用于镜像，不影响后台运行。
  useEffect(() => {
    const sid = diagMode === 'offline' ? offlineSessionId.trim() : sessionId;
    if (!sid) return;
    const cur = getLive(sid);
    if (cur) applyLive(cur);
    return subscribeLive(sid, applyLive);
  }, [diagMode, sessionId, offlineSessionId, applyLive]);

  const handleStartNew = useCallback(() => {
    // 仅复位表单/右栏视图，不中止后台诊断流（界面切换不再取消诊断）
    setView('new');
    setSelectedId(null);
    setReportText('');
    setErrorMsg('');
    setStage('');
    setStatus('idle');
    displayViewRef.current = 'new';
  }, []);

  const handleStart = useCallback(async () => {
    const isOffline = diagMode === 'offline';
    let targetSession = isOffline ? offlineSessionId.trim() : sessionId;
    // 离线模式：日志为必填项；会话 id 留空则自动生成一个合法格式 id，
    // 用于报告落盘分组与历史检索（后端仅要求 id 为合法 path component）。
    if (isOffline) {
      if (logFiles.length === 0) {
        setErrorMsg('离线诊断必须上传/导入至少一份日志');
        setView('new');
        return;
      }
      if (!targetSession) {
        targetSession = `offline-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
        setOfflineSessionId(targetSession);
      }
    } else if (!targetSession) {
      return;
    }

    // 本地渲染切到「诊断中」视图；真正的 SSE 连接交由 module-level 控制器持有，
    // 不受本组件卸载/视图切换影响（界面切换不会中断诊断）。
    setStatus('running');
    setReportText('');
    setErrorMsg('');
    setStage('signal_detect');
    setElapsed(0);
    setView('new');
    setSelectedId(null);
    displayViewRef.current = 'new';
    startDiagnosis(targetSession, {
      userNote: userNote || undefined,
      mode,
      logFiles,
      offline: isOffline || undefined,
    });
  }, [diagMode, sessionId, offlineSessionId, userNote, mode, logFiles, startDiagnosis]);

  // 移除单个已选日志文件（索引定位）
  const removeFile = useCallback((index: number) => {
    setLogFiles((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // 下载当前渲染的诊断报告（markdown 原文，含正文全部内容）
  const handleDownload = useCallback(() => {
    if (!reportText) return;
    const blob = new Blob([reportText], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const sid = (diagMode === 'offline' ? offlineSessionId : sessionId).trim() || 'session';
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    a.href = url;
    a.download = `诊断报告-${sid.slice(0, 24)}-${stamp}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [reportText, diagMode, offlineSessionId, sessionId]);

  const handleCancel = useCallback(() => {
    // 取消「实际在跑」的诊断：以 live 仓库 running 条目的 sessionId 为唯一真值，
    // 不依赖 diagMode/sessionId/offlineSessionId——这些状态可能在诊断进行中被用户改动
    // （切 tab / 改下拉），导致算出的 sid 与 running Map 的 key 对不上、cancel 失效。
    // 兜底：live 未就绪时直接取消全部在跑任务。
    const sid = live?.status === 'running' ? live.sessionId : undefined;
    if (sid) cancelDiagnosis(sid);
    else cancelAllDiagnoses();
    setStatus('idle');
    setStage('');
    setReportText('');
    setLive(null);
    displayViewRef.current = 'empty';
  }, [live]);

  // 在线/离线 tab 切换：复位表单/渲染区，但不中止后台诊断流（之前 abort 会误杀正在跑的诊断）
  const handleModeSwitch = useCallback((mode: DiagMode) => {
    setDiagMode(mode);
    setReportText('');
    setSelectedId(null);
    setErrorMsg('');
    setStage('');
    setStatus('idle');
    setView('empty');
    displayViewRef.current = 'empty';
  }, []);

  const fmtTime = (ts: string) => {
    if (!ts) return '';
    const n = Number(ts);
    if (!n) return ts;
    const d = new Date(n * 1000);
    return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
  };

  // 进行中诊断（来自 diagnosisLive 仓库）的实时镜像，用于在左侧列表顶部展示「诊断中…」
  const liveRunning = live && live.status === 'running' ? live : null;

  return (
    <div className={css.root}>
      <aside className={css.sidebar}>
        <div className={css.sidebarHeader}>
          <div className={css.modeTabs} role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={diagMode === 'online'}
              className={`${css.modeTab} ${diagMode === 'online' ? css.modeTabActive : ''}`}
              disabled={status === 'running'}
              onClick={() => { handleModeSwitch('online'); }}
            >在线诊断</button>
            <button
              type="button"
              role="tab"
              aria-selected={diagMode === 'offline'}
              className={`${css.modeTab} ${diagMode === 'offline' ? css.modeTabActive : ''}`}
              disabled={status === 'running'}
              onClick={() => { handleModeSwitch('offline'); }}
            >离线诊断</button>
          </div>
          {diagMode === 'online' ? (
            <select
              className={css.sessionSelect}
              value={sessionId}
              disabled={status === 'running'}
              onChange={(e) => { setSessionId(e.target.value); }}
              title="选择诊断目标会话"
            >
              {sessions.map((s) => (
                <option key={s.session_id} value={s.session_id}>
                  {s.title ? `${s.title} (${s.session_id.slice(0, 12)})` : s.session_id.slice(0, 20)}
                </option>
              ))}
            </select>
          ) : (
            <input
              type="text"
              className={css.sessionInput}
              value={offlineSessionId}
              disabled={status === 'running'}
              onChange={(e) => { setOfflineSessionId(e.target.value); }}
              placeholder="手输会话 id（可选，留空自动生成）"
              title="离线诊断：手输会话 id，不查 trace，仅凭日志诊断；留空将自动生成"
            />
          )}
          <button type="button" className={css.newBtn} onClick={handleStartNew} disabled={status === 'running'}>+ 新建诊断</button>
        </div>
        <div className={css.list}>
          {liveRunning && (
            <button
              type="button"
              className={`${css.listItem} ${css.liveItem}`}
              onClick={() => { applyLive(liveRunning); }}
              title="诊断进行中，点击查看实时流式输出"
            >
              <div className={css.listItemTime}>诊断中…</div>
              <div className={css.listItemSummary}>{liveRunning.stage || '正在分析日志'}</div>
            </button>
          )}
          {loadingList ? (
            <div className={css.listHint}>加载中…</div>
          ) : reports.length === 0 && !liveRunning ? (
            <div className={css.listHint}>暂无历史诊断</div>
          ) : (
            reports.map((r) => (
              <button
                key={r.report_id}
                type="button"
                className={`${css.listItem} ${selectedId === r.report_id ? css.listItemActive : ''}`}
                onClick={() => { handleSelect(r); }}
              >
                <div className={css.listItemTime}>{fmtTime(r.created_at)}</div>
                <div className={css.listItemSummary}>{r.summary || '(无摘要)'}</div>
              </button>
            ))
          )}
        </div>
      </aside>
      <section className={css.main}>
        {view === 'empty' ? (
          <div className={css.placeholder}>选择一条历史诊断，或点「新建诊断」开始分析</div>
        ) : (
          <div className={css.content}>
            <div className={css.form}>
              <label className={css.field}>
                <span>问题描述（可选）：</span>
                <textarea
                  className={css.note}
                  rows={2}
                  placeholder="发生了什么、预期是什么、有什么规律"
                  value={userNote}
                  onChange={(e) => { setUserNote(e.target.value); }}
                  disabled={status === 'running'}
                />
              </label>
              <div className={css.formRow}>
                <label className={css.fieldInline}>
                  <span>场景：</span>
                  <select value={mode} onChange={(e) => { setMode(e.target.value as DiagnosisMode); }} disabled={status === 'running'}>
                    <option value="error">报错</option>
                    <option value="interrupt">中断</option>
                    <option value="unexpected">不符合预期</option>
                  </select>
                </label>
                <div className={css.fieldInline}>
                  <span>{diagMode === 'offline' ? '日志（必填）：' : '上传日志（可选）：'}</span>
                  <div className={css.filePicker}>
                    <button
                      type="button"
                      className={css.fileBtn}
                      onClick={() => { setPickerOpen((v) => !v); }}
                      disabled={status === 'running'}
                      title="上传日志文件或选择整个日志目录"
                    >+ 添加文件或目录</button>
                    {pickerOpen && (
                      <>
                        <div className={css.pickerBackdrop} onClick={() => { setPickerOpen(false); }} />
                        <div className={css.pickerMenu}>
                          <label className={css.pickerItem}>
                            上传文件
                            <input
                              type="file"
                              multiple
                              accept=".log,.txt,.zip"
                              className={css.pickerInput}
                              onChange={(e) => {
                                setLogFiles((prev) => [...prev, ...Array.from(e.target.files ?? [])]);
                                setPickerOpen(false);
                              }}
                              disabled={status === 'running'}
                            />
                          </label>
                          <label className={css.pickerItem}>
                            选择目录
                            <input
                              type="file"
                              multiple
                              className={css.pickerInput}
                              disabled={status === 'running'}
                              {...DIR_ATTRS}
                              onChange={(e) => {
                                const files = Array.from(e.target.files ?? []);
                                // 目录导入按后缀过滤（与上传校验一致），累加到已选文件
                                const allowed = files.filter((f) => /\.(log|txt|zip)$/i.test(f.name));
                                setLogFiles((prev) => [...prev, ...allowed]);
                                setPickerOpen(false);
                              }}
                            />
                          </label>
                        </div>
                      </>
                    )}
                    {logFiles.length > 0 && (
                      <div className={css.fileList}>
                        <div className={css.fileListHead}>已选 {logFiles.length} 个文件</div>
                        {logFiles.map((f, i) => (
                          <div key={`${f.name}-${i}`} className={css.fileItem}>
                            <span className={css.fileName} title={f.webkitRelativePath || f.name}>
                              {f.webkitRelativePath || f.name}
                            </span>
                            <button
                              type="button"
                              className={css.fileRemove}
                              onClick={() => { removeFile(i); }}
                              disabled={status === 'running'}
                              title="移除该文件"
                              aria-label="移除该文件"
                            >×</button>
                          </div>
                        ))}
                        <button
                          type="button"
                          className={css.fileClear}
                          onClick={() => { setLogFiles([]); }}
                          disabled={status === 'running'}
                        >清空</button>
                      </div>
                    )}
                  </div>
                </div>
              </div>
              <div className={css.actions}>
                {status === 'running' ? (
                  <button type="button" className={css.cancel} onClick={handleCancel}>取消</button>
                ) : (
                  <button
                    type="button"
                    className={css.start}
                    onClick={handleStart}
                    disabled={diagMode === 'online' ? !sessionId : logFiles.length === 0}
                  >开始分析</button>
                )}
              </div>
            </div>
            {status === 'running' ? (
              <div className={css.stage}>进行中：{stage || '初始化'}…（已耗时 {fmtElapsed(elapsed)}）</div>
            ) : null}
            {status === 'done' ? (
              <div className={css.stage}>诊断完成，耗时 {fmtElapsed(elapsed)}</div>
            ) : null}
            {errorMsg ? (
              <div className={css.error}>{errorMsg}</div>
            ) : null}
            {reportText ? (
              <>
                <div className={css.reportToolbar}>
                  <button
                    type="button"
                    className={css.downloadBtn}
                    onClick={handleDownload}
                    title="下载诊断报告（Markdown）"
                  >⬇ 下载报告</button>
                </div>
                <div className={css.report}>
                  <ReactMarkdown>{reportText}</ReactMarkdown>
                </div>
              </>
            ) : null}
          </div>
        )}
      </section>
    </div>
  );
}
