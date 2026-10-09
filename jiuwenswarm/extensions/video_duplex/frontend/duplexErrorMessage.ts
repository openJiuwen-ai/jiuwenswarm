type FailureScope = 'session' | 'task';

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
    action = '请检查服务额度和限流状态，额度恢复后重试';
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

  return `原因：${detail}。结果：${result}。解决方式：${action}。`;
}
