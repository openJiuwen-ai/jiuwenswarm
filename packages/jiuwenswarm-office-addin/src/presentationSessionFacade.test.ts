import { describe, expect, it, vi } from 'vitest';

import type { SessionId } from './clientAttachment';
import type { DocumentId } from './documentIdentity';
import { PresentationSessionFacade } from './presentationSessionFacade';

const S1 = 'session-1' as SessionId;

describe('PresentationSessionFacade', () => {
  it('creates a conversation of the PowerPoint app', async () => {
    const request = vi.fn().mockResolvedValue({ session_id: 'session-1' });
    expect(await new PresentationSessionFacade({ request }).create('model-under-test')).toBe('session-1');
    expect(request).toHaveBeenCalledWith('session.create', {
      create_token: expect.any(String),
      mode: 'agent.work.normal',
      work_mode: 'work',
      is_swarm: false,
      persist_session: false,
      title: '',
      model_name: 'model-under-test',
      client_app: 'office-powerpoint',
    });
  });

  it('refuses a session.create answer without a session_id', async () => {
    const request = vi.fn().mockResolvedValue({});
    await expect(new PresentationSessionFacade({ request }).create('model')).rejects.toThrow(/session_id/);
  });

  it('lists the document\'s conversations as the backend orders them', async () => {
    const request = vi.fn().mockResolvedValue({ conversations: [
      { session_id: 'session-2', title: '改第 3 页', last_message_at: 1762230000.5 },
      { session_id: 'session-1', title: '', last_message_at: 1762200000 },
    ] });
    const listed = await new PresentationSessionFacade({ request })
      .conversations('3f1c2a9e-7b4d-4e8a-9c21-5d6f7a8b9c0d' as DocumentId);
    expect(request).toHaveBeenCalledWith('client.conversations',
      { client_app: 'office-powerpoint', resource_key: '3f1c2a9e-7b4d-4e8a-9c21-5d6f7a8b9c0d' });
    expect(listed).toEqual([
      { sessionId: 'session-2', title: '改第 3 页', lastMessageAt: 1762230000.5 },
      { sessionId: 'session-1', title: '', lastMessageAt: 1762200000 },
    ]);
  });

  it('sends text as an ordinary chat.send of the conversation', async () => {
    const request = vi.fn().mockResolvedValue({});
    await new PresentationSessionFacade({ request }).sendText(S1, 'hello');
    expect(request).toHaveBeenCalledWith(
      'chat.send',
      { session_id: 'session-1', content: 'hello', mode: 'agent.work.normal', work_mode: 'work' },
      { stream: true, timeoutMs: 60_000 },
    );
  });

  it('persists images before chat.send and sends only their paths', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({
        media_items: [
          { type: 'image', filename: 'shot.png', mime_type: 'image/png', path: '/u/shot.png', size_bytes: 12 }],
      })
      .mockResolvedValueOnce({});
    await new PresentationSessionFacade({ request }).sendText(S1, 'what color is this?', [
      { filename: 'shot.png', mimeType: 'image/png', base64Data: 'QUJD' },
    ]);
    expect(request.mock.calls[0]).toEqual([
      'media.persist',
      { session_id: 'session-1', content: '',
        media_items: [{ type: 'image', filename: 'shot.png', mimeType: 'image/png', base64Data: 'QUJD' }] },
      { timeoutMs: 60_000 },
    ]);
    const image = { filename: 'shot.png', mime_type: 'image/png', path: '/u/shot.png', size_bytes: 12 };
    expect(request.mock.calls[1][1]).toEqual({
      session_id: 'session-1', content: 'what color is this?', mode: 'agent.work.normal', work_mode: 'work',
      media_items: [{ type: 'image', ...image }], files: { uploaded_images: [image] },
    });
  });

  it('does not send when media.persist returns no usable image', async () => {
    const request = vi.fn().mockResolvedValue({ media_items: [{ type: 'image', filename: 'x.png' }] });
    await expect(new PresentationSessionFacade({ request }).sendText(S1, 'hi', [
      { filename: 'x.png', mimeType: 'image/png', base64Data: 'QUJD' },
    ])).rejects.toThrow(/media\.persist/);
    expect(request).toHaveBeenCalledOnce();
  });
});
