/**
 * 聊天文件卡片是否按 Skill 包展示「保存」按钮。
 * 与 MessageItem.isSkillPackageFile 保持同一判定。
 */
export type SkillPackageFileLike = {
  name?: string;
  path?: string;
  is_skill_package?: boolean;
};

export function isSkillPackageFile(file: SkillPackageFileLike): boolean {
  if (file.is_skill_package === true) {
    return true;
  }
  const candidates = [file.name, file.path].filter(Boolean) as string[];
  for (const candidate of candidates) {
    const base = candidate.replace(/\\/g, '/').split('/').pop()?.toLowerCase() || '';
    if (base.endsWith('.skill.zip') || base.endsWith('.skill')) {
      return true;
    }
  }
  return false;
}
