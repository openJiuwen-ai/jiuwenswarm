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

/** 素材列表展示用的文件名：引用名（asset.name，通常不含扩展名，聊天框里 @ 它用的就是这个，
 *  不能变）加上从 path 取回的扩展名，这样列表里看到的是完整文件名（如 "Panda_Cub.png"）。
 *  name 本身是用户可编辑的自由文本，重命名输入框不剥离扩展名，用户可能已经把名字改成
 *  "mybottle.png" 这种带扩展名的形式——这时不能再拼一次，否则显示成 "mybottle.png.png"。 */
export function displayFileName(asset: { name: string; path: string }): string {
  const base = asset.path.split(/[\\/]/).pop() ?? '';
  const dot = base.lastIndexOf('.');
  const ext = dot > 0 ? base.slice(dot) : '';
  if (ext && asset.name.toLowerCase().endsWith(ext.toLowerCase())) return asset.name;
  return `${asset.name}${ext}`;
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
 *
 * 优先按 client_id（item.clientId -> asset.client_id，草稿自己的 id，session_assets.py 的
 * 注册幂等键）精确匹配：同一路径现在可以合法地背后站着好几条素材（故意再次附上同一个文件，
 * 和重新附上一张图片一样）。只按路径对会猜中"当前随便哪一条"，同一路径的旧消息会被改成显示
 * 成后来那次重新附上时建出的名字——这正是本函数要避免的那类问题，不能自己在这里重犯。
 * client_id 没命中（老消息、或 agent 产出文件没有这个概念）才退回按路径猜，聊胜于无。
 * 已经有 displayName、或两种键都对不上的条目原样返回；全部无需改动时返回原数组以保持引用稳定。
 */
export function resolveAssetDisplayNames<T extends { path?: string; displayName?: string; clientId?: string }>(
  items: readonly T[],
  assets: readonly (NamedAsset & { client_id?: string })[],
): T[] {
  if (items.length === 0 || assets.length === 0) return items as T[];
  const nameByClientId = new Map<string, string>();
  const nameByPath = new Map<string, string>();
  for (const asset of assets) {
    if (!asset.name) continue;
    if (asset.client_id) nameByClientId.set(asset.client_id, asset.name);
    if (asset.path) nameByPath.set(normalizePath(asset.path), asset.name);
  }
  if (nameByClientId.size === 0 && nameByPath.size === 0) return items as T[];
  let changed = false;
  const next = items.map((item) => {
    if (item.displayName) return item;
    const name =
      (item.clientId ? nameByClientId.get(item.clientId) : undefined) ??
      (item.path ? nameByPath.get(normalizePath(item.path)) : undefined);
    if (!name) return item;
    changed = true;
    return { ...item, displayName: name };
  });
  return changed ? next : (items as T[]);
}
