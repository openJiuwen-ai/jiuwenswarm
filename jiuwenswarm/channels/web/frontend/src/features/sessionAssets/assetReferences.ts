/** findReferencedAssets/withAssetReferenceNote 只用得到这三个字段，所以入参放宽成这个形状：
 *  已经登记到后端的 SessionAsset 天然满足它，还没登记完（这条消息刚上传、正在落盘）的文件也
 *  能现拼一个出来传进去，不用等注册往返打完才能被 @ 引用。 */
export interface NamedAsset {
  name: string;
  path: string;
  kind: string;
}

const WORD_CHAR = /[\p{L}\p{N}_]/u;

/** 文本里被 @名称 引用到的素材。名称可以含空格/中文，所以按名称长度从长到短匹配，
 *  并要求 @名称 后面不是字母数字（避免 "@fox" 误匹配 "@foxes"）。 */
export function findReferencedAssets<T extends NamedAsset>(text: string, assets: readonly T[]): T[] {
  if (!text.includes('@') || assets.length === 0) return [];
  const lowered = text.toLowerCase();
  const found: T[] = [];
  const byLength = [...assets].sort((a, b) => b.name.length - a.name.length);
  const consumed: Array<[number, number]> = [];
  for (const asset of byLength) {
    const token = `@${asset.name.toLowerCase()}`;
    let from = 0;
    while (from < lowered.length) {
      const index = lowered.indexOf(token, from);
      if (index < 0) break;
      const end = index + token.length;
      const next = lowered.charAt(end);
      const overlaps = consumed.some(([s, e]) => index < e && end > s);
      if (!overlaps && (next === '' || !WORD_CHAR.test(next))) {
        consumed.push([index, end]);
        if (!found.includes(asset)) found.push(asset);
      }
      from = end;
    }
  }
  return found;
}

/** 提交给 agent 的文本：在原文后面附上每个被引用素材对应的文件路径，agent 据此把文件当作
 *  参考图/首帧等传给生成工具。没有引用时原样返回。 */
export function withAssetReferenceNote<T extends NamedAsset>(text: string, assets: readonly T[]): string {
  const referenced = findReferencedAssets(text, assets);
  if (referenced.length === 0) return text;
  const lines = referenced.map((asset) => `@${asset.name} = ${asset.path} (${asset.kind})`);
  return `${text}\n\n[引用素材]\n${lines.join('\n')}`;
}

/** 文件名去掉扩展名，作为素材默认名字——和后端 register 里的默认命名规则（Path(path).stem）一致，
 *  这样这条消息里刚上传、还没走完注册往返的文件，也能用同一个默认名字被 @ 到。 */
export function stemFilename(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? filename;
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(0, dot) : base;
}

/** MIME 类型 -> 素材种类，和后端 session_assets.py 的 _kind_of 对齐（用于笔记里的展示文本，
 *  不影响引用匹配本身）。 */
export function assetKindFromMime(mimeType: string): string {
  const type = (mimeType || '').toLowerCase();
  if (type.startsWith('image/')) return 'image';
  if (type.startsWith('video/')) return 'video';
  if (type.startsWith('audio/')) return 'audio';
  return 'document';
}

export const ASSET_NAME_MAX = 60;

/** 和后端 validate_asset_name 一致：去首尾空白并折叠内部空白；不能为空、不超长、不含 @ 或换行。 */
export function validateAssetName(input: string): { name: string } | { error: 'invalid' } {
  const name = input.split(/\s+/).filter(Boolean).join(' ');
  if (!name || name.length > ASSET_NAME_MAX || /[@\r\n\t]/.test(input)) return { error: 'invalid' };
  return { name };
}

/** 路径归一化：忽略大小写和斜杠方向（Windows 路径），可以直接当 Map/Set 的 key 用。 */
export function normalizePath(value: string): string {
  return value.split(String.fromCharCode(92)).join('/').toLowerCase();
}

/** 路径比较：忽略大小写和斜杠方向（Windows 路径）。 */
export function samePath(a: string, b: string): boolean {
  return normalizePath(a) === normalizePath(b);
}

/**
 * 把会话素材表里用户起的名称补到历史消息的附件上。
 *
 * `displayName` 不随消息落盘，所以刷新页面后，改过名的附件会退化成原始文件名
 * （如 `EMXN1y8qOw....webp`）。历史记录里存了绝对路径，素材表里也有路径，按路径对回去即可。
 * 已经有 displayName、或路径对不上的条目原样返回；全部无需改动时返回原数组以保持引用稳定。
 */
export function resolveAssetDisplayNames<T extends { path?: string; displayName?: string }>(
  items: readonly T[],
  assets: readonly NamedAsset[],
): T[] {
  if (items.length === 0 || assets.length === 0) return items as T[];
  const nameByPath = new Map<string, string>();
  for (const asset of assets) {
    if (asset.name && asset.path) nameByPath.set(normalizePath(asset.path), asset.name);
  }
  if (nameByPath.size === 0) return items as T[];
  let changed = false;
  const next = items.map((item) => {
    if (item.displayName || !item.path) return item;
    const name = nameByPath.get(normalizePath(item.path));
    if (!name) return item;
    changed = true;
    return { ...item, displayName: name };
  });
  return changed ? next : (items as T[]);
}
