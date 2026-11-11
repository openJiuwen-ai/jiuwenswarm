import type { SessionId } from './clientAttachment';
import type { DocumentId } from './documentIdentity';
import { isRecord, type GatewayClient } from './gatewayClient';
import { POWERPOINT_CLIENT_APP } from './presentationContract';

/** A user-picked image, base64-encoded, waiting to be persisted server-side. */
export interface OutgoingImageAttachment {
  filename: string;
  mimeType: string;
  base64Data: string;
}

export interface ResourceConversation {
  sessionId: SessionId;
  title: string;
  /** Seconds since the epoch. */
  lastMessageAt: number;
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
export class PresentationSessionFacade {
  constructor(private readonly gateway: Pick<GatewayClient, 'request'>) {}

  /** A new conversation of the PowerPoint app (``client_app``), which shows only in this client. */
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
      client_app: POWERPOINT_CLIENT_APP,
    });
    if (typeof created.session_id !== 'string' || !created.session_id) {
      throw new Error('session.create 未返回 session_id');
    }
    return created.session_id as SessionId;
  }

  /** The user's conversations of a document (``client.conversations``), the most recently active first. */
  async conversations(documentId: DocumentId): Promise<ResourceConversation[]> {
    const response = await this.gateway.request(
      'client.conversations', { client_app: POWERPOINT_CLIENT_APP, resource_key: documentId });
    if (!Array.isArray(response.conversations)) throw new Error('client.conversations 未返回 conversations');
    return response.conversations.filter(isRecord).flatMap(item => (
      typeof item.session_id === 'string' && item.session_id
        ? [{ sessionId: item.session_id as SessionId, title: typeof item.title === 'string' ? item.title : '',
          lastMessageAt: typeof item.last_message_at === 'number' ? item.last_message_at : 0 }]
        : []));
  }

  async sendText(sessionId: SessionId, content: string, images: OutgoingImageAttachment[] = []): Promise<void> {
    const mediaItems = images.length ? await this.persistImages(sessionId, images) : undefined;
    const uploadedImages = mediaItems?.map(({ filename, path, mime_type, size_bytes }) => (
      { filename, path, mime_type, size_bytes }));
    await this.gateway.request(
      'chat.send',
      {
        session_id: sessionId,
        content,
        mode: 'agent.work.normal',
        work_mode: 'work',
        ...(mediaItems ? { media_items: mediaItems, files: { uploaded_images: uploadedImages } } : {}),
      },
      { stream: true, timeoutMs: 60_000 },
    );
  }

  async interrupt(
    sessionId: SessionId, intent: 'pause' | 'resume' | 'cancel' | 'supplement', newInput?: string,
  ): Promise<void> {
    await this.gateway.request('chat.interrupt', {
      session_id: sessionId,
      intent,
      ...(intent === 'supplement' ? { new_input: newInput ?? '' } : {}),
    });
  }

  /**
   * Images travel as base64 only once, to ``media.persist``, which writes them
   * under the conversation's uploads and hands back their paths, as the web
   * channel does. ``chat.send`` then carries the paths in ``media_items`` (for
   * the multimodal rail) and ``files.uploaded_images`` (for the user turn).
   */
  private async persistImages(
    sessionId: SessionId, images: OutgoingImageAttachment[],
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
