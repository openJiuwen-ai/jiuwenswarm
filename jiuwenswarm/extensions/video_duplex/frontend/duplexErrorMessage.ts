type FailureScope = 'session' | 'task';

/** Add protocol identifiers at the display boundary, without changing error classification. */
export function duplexErrorDetail(value: unknown, context: Record<string, unknown> = {}): string {
  const record = value !== null && typeof value === 'object' ? value as Record<string, unknown> : {};
  const message = typeof value === 'string' ? value
    : String(record.message || record.detail || record.error || '服务未提供具体原因');
  const fields = { type: record.type || record.name, code: record.code, param: record.param,
    event_id: record.event_id, request_id: record.request_id,
    ...Object.fromEntries(Object.entries(context).filter(([, item]) => item !== undefined && item !== null)) };
  const identifiers = Object.entries(fields)
    .filter(([, item]) => (typeof item === 'string' && item !== '') || typeof item === 'number')
    .map(([key, item]) => `${key}=${String(item).slice(0, 200)}`);
  return `${message.slice(0, 1500)}${identifiers.length ? ` [${identifiers.join(', ')}]` : ''}`;
}

export function screenCaptureFailure(error: unknown) {
  const native = error !== null && typeof error === 'object' ? error as { name?: unknown; message?: unknown } : null;
  const errorType = typeof native?.name === 'string' ? native.name : 'UnknownError';
  const nativeMessage = typeof native?.message === 'string' && native.message.trim()
    ? native.message
    : typeof error === 'string' && error.trim() ? error : '(no native message)';
  const reasons: Record<string, string> = {
    NotAllowedError: 'permissionOrCancel',
    AbortError: 'aborted',
    NotReadableError: 'unreadable',
    InvalidStateError: 'invalidState',
    SecurityError: 'security',
  };
  return { errorType, nativeMessage, reasonKey: reasons[errorType] || 'unknown' };
}

/** Keep the service's actual error while explaining its effect and a useful next step. */
export function describeDuplexError(raw: string, scope: FailureScope = 'session'): string {
  const detail = raw.trim() || '服务未提供具体原因';
  if (detail.startsWith('原因：') && detail.includes('结果：') && detail.includes('解决方式：')) return detail;

  const task = scope === 'task';
  let result = task ? '本次任务未完成，无法使用任务结果' : '本次语音交互可能未完成';
  let action = task
    ? '请重试；若再次失败，查看 Jiuwen Core Agent 的任务日志'
    : '请重试；若再次失败，查看全双工服务日志';

  if (/请配置|未配置|missing.*(key|model|url|endpoint)|not configured/i.test(detail)) {
    result = task ? result : '全双工会话无法正常启动或继续';
    action = '请在全双工设置中补全提示所指的配置，然后重新启动会话';
  } else if (/\b(401|403)\b|unauthorized|forbidden|invalid.?api.?key|鉴权|认证失败/i.test(detail)) {
    action = '请检查对应服务的 API Key、访问权限及模型授权，然后重试';
  } else if (/\b429\b|quota|rate.?limit|限流|额度|配额/i.test(detail)) {
    if (/joyai/i.test(detail)) {
      result = '模型供应商额度已耗尽，本次请求被拒';
      action = 'JoyAI 服务商限流（429，每分钟 token 额度已用完），请稍后重试或提高供应商额度';
    } else {
      action = '请检查服务额度和限流状态，额度恢复后重试';
    }
  } else if (/not.?supported|不支持/i.test(detail)) {
    // A runtime capability gap (#5283): the service logs hold nothing useful.
    result = '当前运行环境不支持该功能';
    action = '请关闭屏幕共享后仅使用语音，或换用支持该功能的浏览器';
  } else if (/麦克风|摄像头|屏幕共享|录音|microphone|camera|permission denied|notallowederror/i.test(detail)) {
    result = '本次音视频输入不可用';
    action = '请检查浏览器及系统的设备权限，并确认设备未被其他程序占用';
  } else if (/初始化失败|无法启动|start.failed/i.test(detail)) {
    result = '全双工会话未能启动';
    action = '请检查模型服务地址、网络连接和 Jiuwen 服务日志，然后重试';
  } else if (/连接已断开|连接已关闭|session.closed|websocket|network|timeout|timed out|连接异常|连接超时/i.test(detail)) {
    result = '全双工连接已中断，当前语音输入和回应无法继续';
    action = '请检查网络及模型服务状态，然后重新启动全双工';
  } else if (/response.*failed|响应失败|音频解码失败/i.test(detail)) {
    result = '本次模型回应未完成';
    action = '请重试本轮对话；若再次失败，检查模型服务日志';
  }

  if (!task && /\bTTS\b|语音合成|音频解码|播报响应冲突/i.test(detail)) {
    result = '本次语音播报未完成；请查看文字回应和任务列表确认实际结果';
    action = /\b(400|401|403)\b|voice=|音色/i.test(detail)
      ? '请检查语音合成模型、音色及授权是否匹配；使用请求标识核对服务日志'
      : '请检查音频设备及语音服务；使用请求或响应标识核对日志，避免重复提交任务';
  } else if (!task && /\bASR\b|语音识别|input_audio_transcription/i.test(detail)) {
    result = '本次语音识别未完成，不能据此确认指令已提交';
    action = '请检查麦克风、识别服务和网络；先核对任务列表，再决定是否重说指令';
  } else if (/工具调用|队列操作|任务提交|辅助模型复核|TASK_REQUEST_REJECTED/i.test(detail)) {
    result = '本次操作未能确认完成，部分操作可能已经执行';
    action = '请先核对任务列表和定时任务；使用调用或请求标识核对日志后再重试';
  }

  return `原因：${detail}。结果：${result}。解决方式：${action}。`;
}
