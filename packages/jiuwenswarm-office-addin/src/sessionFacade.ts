import type { SessionId } from './clientAttachment';
import type { DocumentId } from './documentIdentity';
import { isRecord, type GatewayClient, type JsonObject } from './gatewayClient';

/** A user-picked image, base64-encoded, waiting to be persisted server-side. */
export interface OutgoingImageAttachment {
  filename: string;
  mimeType: string;
  base64Data: string;
}

/** The image as a ``data:`` URL, to show it in the pane. */
export function imageDataUrl({ mimeType, base64Data }: OutgoingImageAttachment): string {
  return `data:${mimeType};base64,${base64Data}`;
}

/** A document the user has open with its task pane attached. */
export interface OpenDocument {
  name: string;
  /** What it is, as the attach's ``resource.kind`` gives it: ``presentation``, ``document``, ``workbook``. */
  kind: string;
  /** The document of the conversation that asked. */
  current: boolean;
}

export interface ResourceConversation {
  sessionId: SessionId;
  title: string;
  /** Seconds since the epoch. */
  lastMessageAt: number;
}

/**
 * The document's state a message carries (``text``), and ``sent``, which the app runs once ``chat.send`` took the
 * message: what the state lists as new (the user's changes, their decisions) is then told and not listed again.
 */
export interface ClientState {
  text: string;
  sent(): void;
}

interface PersistedImageAttachment {
  type: 'image';
  filename: string;
  mime_type: string;
  path: string;
  size_bytes?: number;
}

/**
 * The conversation requests of the pane. The backend owns the agent loop and
 * its context; which conversation the pane shows is the attachment's.
 */
export class SessionFacade {
  /**
   * ``clientApp``: the app whose conversations these are, as the backend registers it. ``clientState``: the
   * document's state as the app's ``read_surface_context`` gives it, which each message carries
   * (``chat.send`` ``client_state``) for the backend to show the model with the message.
   */
  constructor(
    private readonly gateway: Pick<GatewayClient, 'request'>,
    private readonly clientApp: string,
    private readonly clientState?: (sessionId: SessionId) => Promise<ClientState>,
  ) {}

  /** A new conversation of the app (``client_app``), which shows only in this client. */
  async create(modelName: string): Promise<SessionId> {
    const created = await this.gateway.request('session.create', {
      create_token: crypto.randomUUID(),
      mode: 'agent.work.normal',
      work_mode: 'work',
      is_swarm: false,
      persist_session: false,
      // Empty, so the history writer titles the conversation after its first user message.
      title: '',
      model_name: modelName,
      client_app: this.clientApp,
    });
    if (typeof created.session_id !== 'string' || !created.session_id) {
      throw new Error('session.create 未返回 session_id');
    }
    return created.session_id as SessionId;
  }

  /** The user's conversations of a document (``client.conversations``), the most recently active first. */
  async conversations(documentId: DocumentId): Promise<ResourceConversation[]> {
    const response = await this.gateway.request(
      'client.conversations', { client_app: this.clientApp, resource_key: documentId });
    if (!Array.isArray(response.conversations)) throw new Error('client.conversations 未返回 conversations');
    return response.conversations.filter(isRecord).flatMap(item => (
      typeof item.session_id === 'string' && item.session_id
        ? [{ sessionId: item.session_id as SessionId, title: typeof item.title === 'string' ? item.title : '',
          lastMessageAt: typeof item.last_message_at === 'number' ? item.last_message_at : 0 }]
        : []));
  }

  /** The models a message can run on (``models.list``), and the server's active one, as the web chat picks it. */
  async models(): Promise<{ names: string[]; active: string }> {
    const response = await this.gateway.request('models.list');
    const names = (Array.isArray(response.models) ? response.models : []).flatMap(model => (
      isRecord(model) && typeof model.model_name === 'string' && model.model_name ? [model.model_name] : []));
    const active = typeof response.active_model === 'string' && response.active_model ? response.active_model : names[0];
    if (!active) throw new Error('没有可用于对话的模型');
    return { names: names.includes(active) ? names : [active, ...names], active };
  }

  /** The model a new conversation runs on. */
  async defaultModel(): Promise<string> {
    return (await this.models()).active;
  }

  /** The latest page of a conversation's history, oldest first, without reasoning text. */
  async history(sessionId: SessionId): Promise<JsonObject[]> {
    const response = await this.gateway.request('client.conversation.messages', { target_session_id: sessionId });
    if (!Array.isArray(response.messages)) throw new Error('client.conversation.messages 未返回 messages');
    return response.messages.filter(isRecord);
  }

  /** The user's attached documents (``client.list``), by the conversation each is attached to. */
  async openDocuments(sessionId: SessionId): Promise<Map<string, OpenDocument>> {
    const response = await this.gateway.request('client.list', { session_id: sessionId });
    const attachments = Array.isArray(response.attachments) ? response.attachments.filter(isRecord) : [];
    return new Map(attachments.flatMap(item => (
      typeof item.session_id === 'string' && typeof item.display_name === 'string'
        ? [[item.session_id, { name: item.display_name, kind: typeof item.kind === 'string' ? item.kind : '',
          current: item.is_current === true }] as const] : [])));
  }

  /** Answer the question a paused turn asks (``chat.ask_user_question``); the turn goes on. */
  async answer(sessionId: SessionId, { requestId, interruptId, source }: {
    requestId: string; interruptId: string; source: string;
  }, answers: JsonObject[]): Promise<void> {
    await this.gateway.request('chat.send', {
      session_id: sessionId,
      query: '',
      mode: 'agent.work.normal',
      work_mode: 'work',
      request_id: requestId,
      answers,
      source,
      ...(interruptId ? { interrupt_id: interruptId } : {}),
    });
  }

  /** A message, on ``modelName`` when given (``chat.send`` ``model_name``, as the web chat's model picker sends it). */
  async sendText(
    sessionId: SessionId, content: string, images: readonly OutgoingImageAttachment[] = [], modelName?: string,
  ): Promise<void> {
    const mediaItems = images.length ? await this.persistImages(sessionId, images) : undefined;
    const uploadedImages = mediaItems?.map(({ filename, path, mime_type, size_bytes }) => (
      { filename, path, mime_type, size_bytes }));
    const state = this.clientState ? await this.clientState(sessionId) : undefined;
    await this.gateway.request(
      'chat.send',
      {
        session_id: sessionId,
        content,
        mode: 'agent.work.normal',
        work_mode: 'work',
        ...(modelName ? { model_name: modelName } : {}),
        ...(mediaItems ? { media_items: mediaItems, files: { uploaded_images: uploadedImages } } : {}),
        ...(state === undefined ? {} : { client_state: state.text }),
      },
      { stream: true, timeoutMs: 60_000 },
    );
    state?.sent();
  }

  /** Stop the running turn; a supplement starts the next one on ``modelName``, as ``chat.send`` does. */
  async interrupt(
    sessionId: SessionId, intent: 'pause' | 'resume' | 'cancel' | 'supplement', newInput?: string, modelName?: string,
  ): Promise<void> {
    await this.gateway.request('chat.interrupt', {
      session_id: sessionId,
      intent,
      ...(intent === 'supplement' ? { new_input: newInput ?? '' } : {}),
      ...(modelName ? { model_name: modelName } : {}),
    });
  }

  /**
   * Images travel as base64 only once, to ``media.persist``, which writes them
   * under the conversation's uploads and hands back their paths, as the web
   * channel does. ``chat.send`` then carries the paths in ``media_items`` (for
   * the multimodal rail) and ``files.uploaded_images`` (for the user turn).
   */
  private async persistImages(
    sessionId: SessionId, images: readonly OutgoingImageAttachment[],
  ): Promise<PersistedImageAttachment[]> {
    const response = await this.gateway.request(
      'media.persist',
      {
        session_id: sessionId,
        content: '',
        media_items: images.map(({ filename, mimeType, base64Data }) => (
          { type: 'image', filename, mimeType, base64Data })),
      },
      { timeoutMs: 60_000 },
    );
    const persisted = Array.isArray(response.media_items) ? response.media_items.filter(isPersistedImage) : [];
    if (!persisted.length) throw new Error('media.persist 未返回可用的图片路径');
    return persisted.map(({ filename, mime_type, path, size_bytes }) => (
      { type: 'image', filename, mime_type, path, size_bytes }));
  }
}

function isPersistedImage(value: unknown): value is PersistedImageAttachment {
  return isRecord(value)
    && (value.type === undefined || value.type === 'image')
    && typeof value.filename === 'string' && value.filename.length > 0
    && typeof value.mime_type === 'string' && value.mime_type.startsWith('image/')
    && typeof value.path === 'string' && value.path.length > 0
    && (value.size_bytes === undefined || typeof value.size_bytes === 'number');
}
