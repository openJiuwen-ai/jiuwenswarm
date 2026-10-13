import type { ArtifactItem } from './artifactCollection';

/**
 * 临时登记「要预览、但不在消息产物列表里」的文件。
 *
 * 产物面板是按 id 从 `buildArtifacts(messages)` 里查条目的，而任务素材（session assets）由后端
 * 按会话登记，只有走过 `send_file_to_user` 的那些才会出现在消息里。点素材时先把它换算成产物形态
 * 登记到这里，产物面板查不到列表就能查这里，于是素材也能像产物一样打开预览。
 *
 * 只做内存映射，刷新页面即失效——刷新后素材要么已经在产物里，要么重新点一次即可。
 */
const previewableById = new Map<string, ArtifactItem>();

export function registerPreviewableArtifact(item: ArtifactItem): void {
  previewableById.set(item.id, item);
}

export function resolvePreviewableArtifact(id: string | undefined): ArtifactItem | null {
  if (!id) return null;
  return previewableById.get(id) ?? null;
}
