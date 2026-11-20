/**
 * 协议处理结果：区分“拒绝”（数据非法，整帧丢弃）与“不支持”（版本/模式不认识）。
 * 挂载与实例管理层据此决定保留旧界面还是记录错误，协议层不直接改实例状态。
 */

export type WebUIProtocolFailureKind = 'reject' | 'unsupported';

export type WebUIProtocolErrorCode =
  | 'SCHEMA_ERROR'
  | 'UNKNOWN_COMPONENT'
  | 'FRAME_TOO_LARGE'
  | 'UNSUPPORTED_SCHEMA_VERSION'
  | 'UNSUPPORTED_MODE';

export interface WebUIProtocolFailure {
  kind: WebUIProtocolFailureKind;
  code: WebUIProtocolErrorCode;
  message: string;
}

export type WebUIProtocolResult<T> =
  | { ok: true; value: T }
  | { ok: false; failure: WebUIProtocolFailure };

export function protocolOk<T>(value: T): WebUIProtocolResult<T> {
  return { ok: true, value };
}

export function protocolReject<T = never>(
  code: WebUIProtocolErrorCode,
  message: string,
): WebUIProtocolResult<T> {
  return { ok: false, failure: { kind: 'reject', code, message } };
}

export function protocolUnsupported<T = never>(
  code: WebUIProtocolErrorCode,
  message: string,
): WebUIProtocolResult<T> {
  return { ok: false, failure: { kind: 'unsupported', code, message } };
}
