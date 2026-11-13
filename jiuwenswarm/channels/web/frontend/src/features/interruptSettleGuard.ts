/**
 * interrupt 后的 metadata 快照守卫（bug1112）。
 *
 * pause/cancel ack 已经权威收尾本轮时，worker 运行态快照可能滞后于收尾
 * （leader 流记录未退役、team 轮未排空），session.get_metadata 会继续报
 * processing。若前端允许这个快照回写 isProcessing，停止按钮会在
 * 「点停止 → ack 收尾 → 快照拉回 processing」的环里永久卡死。
 *
 * 守卫语义：interrupt 已收尾的会话内，快照的 processing=true 不得点亮本地
 * 状态；窗口在收到真实 processing_status(true)（带 event_type 的线帧）或
 * 用户发起新一轮发送时结束。断线重连后的首次快照（allowDowngrade）不受
 * 此守卫约束——那是恢复运行态的合法通道。
 */

/**
 * metadata 快照是否允许把本地 processing 拉起来。
 *
 * @param metadataIsProcessing 快照报告的 is_processing
 * @param interruptSettled 本会话是否存在未结束的 interrupt 收尾窗口
 * @param allowDowngrade 断线重连后的首次快照（允许快照纠正运行态）
 */
export function maySnapshotRelightProcessing(
  metadataIsProcessing: boolean,
  interruptSettled: boolean,
  allowDowngrade: boolean,
): boolean {
  if (!metadataIsProcessing) return true;
  if (!interruptSettled) return true;
  // A reconnect snapshot is the legitimate channel to restore a running
  // turn whose terminal events were lost while disconnected.
  return allowDowngrade;
}

/**
 * processing_status(true) 事件帧是否允许点亮 processing。
 *
 * webClient 的 resumeExecutions 会用快照结果合成无 event_type 的
 * processing_status 事件；interrupt 刚收尾时这类合成帧是滞后快照的回声，
 * 不得重新点亮。真实线帧带 event_type，放行并结束收尾窗口。
 */
export function mayProcessingTrueFrameRelight(
  hasEventType: boolean,
  interruptSettled: boolean,
): boolean {
  if (hasEventType) return true;
  return !interruptSettled;
}
