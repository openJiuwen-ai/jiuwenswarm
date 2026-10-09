import assert from 'node:assert/strict';
import test from 'node:test';
import {
  findReferencedAssets,
  resolveAssetDisplayNames,
  samePath,
  validateAssetName,
  withAssetReferenceNote,
} from '../node_modules/.cache/session-assets/assetReferences.mjs';

const asset = (name, path = `C:\\files\\${name}.png`, kind = 'image') => ({
  asset_id: `id-${name}`, name, kind, path, source: 'upload', created_at: 0,
});

test('finds referenced assets, longest name first, case-insensitively', () => {
  const assets = [asset('Oat'), asset('Oat Serum')];
  const found = findReferencedAssets('make a video of @oat serum next to @Oat', assets);
  assert.deepEqual(found.map((a) => a.name), ['Oat Serum', 'Oat']);
});

test('does not match a name that is only a prefix of a longer word', () => {
  assert.deepEqual(findReferencedAssets('see @foxes', [asset('fox')]), []);
  assert.deepEqual(findReferencedAssets('see @fox, then', [asset('fox')]).length, 1);
  assert.deepEqual(findReferencedAssets('看 @狐狸。', [asset('狐狸')]).length, 1);
});

test('no @ or no assets leaves the text untouched', () => {
  assert.equal(withAssetReferenceNote('plain text', [asset('fox')]), 'plain text');
  assert.equal(withAssetReferenceNote('hi @fox', []), 'hi @fox');
  assert.equal(withAssetReferenceNote('hi @nobody', [asset('fox')]), 'hi @nobody');
});

test('appends a path note for each referenced asset once', () => {
  const text = withAssetReferenceNote('use @fox and @fox again', [asset('fox', 'C:\\a\\fox.png')]);
  assert.equal(text, 'use @fox and @fox again\n\n[引用素材]\n@fox = C:\\a\\fox.png (image)');
});

test('validates asset names like the backend', () => {
  assert.deepEqual(validateAssetName('  Oat   Serum '), { name: 'Oat Serum' });
  for (const bad of ['', '   ', 'a@b', 'x'.repeat(61), 'two\nlines']) {
    assert.deepEqual(validateAssetName(bad), { error: 'invalid' });
  }
});

test('compares Windows paths ignoring case and slash direction', () => {
  assert.equal(samePath('C:\\A\\b.png', 'c:/a/B.PNG'), true);
  assert.equal(samePath('C:\\A\\b.png', 'C:\\A\\c.png'), false);
});

// 真实数据：会话 web_1a1102fc6e1_5e63432faddc 的 history.jsonl 与 session_assets.json。
// 附件在历史里只有原始 filename，改成 serum 的名字只存在素材表里，靠路径对回去。
const SERUM_PATH =
  'C:\\Users\\Administrator\\.jiuwenswarm\\agent\\sessions\\web_1a1102fc6e1_5e63432faddc\\uploads\\' +
  'EMXN1y8qOwoGdXBsb2FkEg55bGFiLXN0dW50LXNncBohY2FudmFzL0FnZW5057Sg5p2QL2JvdHRsZSAoMSkucG5n_100x100.webp';
const SERUM_HISTORY_ITEM = {
  type: 'image',
  mime_type: 'image/webp',
  filename: 'EMXN1y8qOwoGdXBsb2FkEg55bGFiLXN0dW50LXNncBohY2FudmFzL0FnZW5057Sg5p2QL2JvdHRsZSAoMSkucG5n_100x100.webp',
  path: SERUM_PATH,
  size_bytes: 528,
};
const SERUM_ASSET = {
  asset_id: 'asset_664b5fff',
  name: 'serum',
  kind: 'image',
  path: SERUM_PATH,
  source: 'upload',
  created_at: 0,
};

test('restores a renamed asset name on history media after a page refresh', () => {
  const [resolved] = resolveAssetDisplayNames([SERUM_HISTORY_ITEM], [SERUM_ASSET]);
  assert.equal(resolved.displayName, 'serum');
  // 真实文件名不受影响：扩展名标签、下载名仍按 filename 走
  assert.equal(resolved.filename, SERUM_HISTORY_ITEM.filename);
  assert.equal(resolved.path, SERUM_PATH);
});

test('matches the path even when the asset list uses forward slashes', () => {
  const [resolved] = resolveAssetDisplayNames(
    [SERUM_HISTORY_ITEM],
    [{ ...SERUM_ASSET, path: SERUM_PATH.replace(/\\/g, '/') }],
  );
  assert.equal(resolved.displayName, 'serum');
});

test('leaves items alone when nothing resolves, keeping array identity', () => {
  const items = [SERUM_HISTORY_ITEM];
  assert.equal(resolveAssetDisplayNames(items, []), items);
  const renamed = resolveAssetDisplayNames(items, [{ ...SERUM_ASSET, path: 'C:\\other\\file.webp' }]);
  assert.equal(renamed, items);
  assert.equal(items[0].displayName, undefined);
});

test('never overrides a displayName that is already present', () => {
  const item = { ...SERUM_HISTORY_ITEM, displayName: 'kept' };
  const [resolved] = resolveAssetDisplayNames([item], [SERUM_ASSET]);
  assert.equal(resolved.displayName, 'kept');
});

test('resolves several attachments and leaves unmatched ones untouched', () => {
  const other = { type: 'image', filename: 'plain.png', path: 'C:\\files\\plain.png' };
  const out = resolveAssetDisplayNames([SERUM_HISTORY_ITEM, other], [SERUM_ASSET]);
  assert.equal(out[0].displayName, 'serum');
  assert.equal(out[1].displayName, undefined);
  assert.equal(out[1], other);
});
