export interface FileAttachment {
  readonly type: "file" | "image";
  readonly path: string;
  readonly filename: string;
  readonly mimeType?: string;
  readonly content?: string; // base64 for images
}

export interface ReqFrame {
  readonly type: "req";
  readonly id: string;
  readonly method: string;
  readonly params: Record<string, unknown> & {
    content?: string;
    mode?: string;
    query?: string;
    session_id?: string;
    attachments?: FileAttachment[];
  };
}

export interface ResFrame {
  readonly type: "res";
  readonly id: string;
  readonly ok: boolean;
  readonly payload: Record<string, unknown>;
  readonly error?: string;
  readonly code?: string;
}

export interface EventFrame {
  readonly type: "event";
  readonly event: string;
  readonly payload: Record<string, unknown>;
  readonly seq?: number;
  readonly stream_id?: string;
}

export type Frame = ReqFrame | ResFrame | EventFrame;

export function isResFrame(frame: Frame): frame is ResFrame {
  return frame.type === "res";
}

export function isEventFrame(frame: Frame): frame is EventFrame {
  return frame.type === "event";
}
