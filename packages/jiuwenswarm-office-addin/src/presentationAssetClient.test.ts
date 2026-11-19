import { describe, expect, it, vi } from 'vitest';

import { downloadAsset } from './presentationAssetClient';

const HELLO_SHA256 = '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824';
const HELLO = { handle: 'oa_down', mime_type: 'image/png', size: 5, sha256: HELLO_SHA256 };
const DOWNLOADS = { oa_down: '/file-api/download?token=down' };

function serving(body: string, status = 200) {
  return vi.fn(async (_url: string, _init: RequestInit) => new Response(body, { status }));
}

describe('downloadAsset', () => {
  it('fetches the granted link without following redirects and hands back the bytes as base64', async () => {
    const fetcher = serving('hello');
    expect(await downloadAsset(HELLO, DOWNLOADS, fetcher)).toBe(btoa('hello'));
    expect(fetcher).toHaveBeenCalledWith('/file-api/download?token=down', { redirect: 'error' });
  });

  it('refuses bytes that are not the published file', async () => {
    await expect(downloadAsset(HELLO, DOWNLOADS, serving('hellO'))).rejects.toThrow(/SHA-256 differs/);
    await expect(downloadAsset({ ...HELLO, size: 6 }, DOWNLOADS, serving('hello'))).rejects.toThrow(/size or SHA-256/);
    await expect(downloadAsset(HELLO, DOWNLOADS, serving('', 404))).rejects.toThrow(/HTTP 404/);
  });

  it.each([
    'https://elsewhere.example/file-api/download?token=x', '//elsewhere.example/file-api/download?token=x',
    '/file-api/download?token=x&redirect=elsewhere', '/file-api/upload?token=x',
  ])('fetches no link but the backend\'s download route: %s', async url => {
    const fetcher = serving('hello');
    await expect(downloadAsset(HELLO, { oa_down: url }, fetcher)).rejects.toThrow(/not a file-api link/);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('needs a link for the handle', async () => {
    await expect(downloadAsset({ ...HELLO, handle: 'oa_other' }, DOWNLOADS, serving('hello')))
      .rejects.toThrow(/no download was granted/);
  });
});
