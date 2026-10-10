/**
 * "我的技能"归属判定——技能管理页（SkillPanel/index.tsx）与手动创建插件的"添加技能"弹窗
 * （ConnectorMarket/CreatePluginPage.tsx）共用同一份规则，避免各自维护一份互相跑偏（2026-08-21
 * 用户明确指出手动创建插件里的"我的技能"比技能管理页少——根因是那边当时简化成了只看
 * source==='local' 一个条件，漏了"来自某个已安装插件的技能"和 is_builtin/is_builtin_source
 * 这两类，跟技能管理页"我的技能"tab 的真实口径不一致）。
 *
 * 判定规则照抄 SkillPanel/index.tsx 原来 filteredSkills + visibleSkills 里
 * activeTab === 'my' 分支的两步过滤，合并成一步（两步顺序可交换：第二步的排除条件不依赖搜索
 * 关键字，跟搜索过滤谁先谁后结果一样）：
 * 1. 候选集：已通过某个插件安装（installedSkillNames 命中）∪ source==='local' ∪
 *    is_builtin===true ∪ is_builtin_source===true ∪ installed===true（后端 with_installed 返回）。
 * 2. 从候选集里排除：is_builtin_source===true，但既没有通过插件安装、后端 installed 也不是 true、source 也不是 'local'
 *    的——这类是"内置技能的源码存在，但用户没真的装它"，不算"我的"。
 */
export function computeMySkills<
  T extends { name: string; source?: string; is_builtin?: boolean; is_builtin_source?: boolean; installed?: boolean },
>(skills: T[], installedSkillNames: Set<string>): T[] {
  return skills.filter((skill) => {
    const installed = installedSkillNames.has(skill.name);
    const isCandidate =
      installed || skill.installed === true || skill.source === 'local' || skill.is_builtin === true || skill.is_builtin_source === true;
    if (!isCandidate) return false;
    if (skill.is_builtin_source && !installed && skill.installed !== true && skill.source !== 'local') return false;
    return true;
  });
}

/** 从"已安装插件"列表反推出"通过插件装进来的技能名"集合——同 SkillPanel/index.tsx 的
 * installedSkillNames 计算方式，供不需要 installedSkillMap 那份完整 Map（只要判断集合归属）的
 * 调用方直接用。plugin.skills 里每一项可能是纯字符串，也可能是 `{name, version}` 对象（后端
 * 两种形状都会返回，见 SkillPanel/index.tsx InstalledPluginItem 类型注释），2026-08-25 之前
 * 这里只处理了字符串形式，遇到对象形式会把整个对象塞进 Set，导致 `.has(name)` 恒为 false——
 * 这个 Set 只被 computeMySkills 的"候选集"判断用到过，被其余 OR 条件（source==='local'/
 * is_builtin 等）掩盖，直到 filterEnabledMySkills 严格依赖它才暴露出来（真机验证时"添加技能"
 * 弹窗直接空了）。 */
export function buildInstalledSkillNames(plugins: { skills: (string | { name: string })[] }[]): Set<string> {
  const set = new Set<string>();
  for (const plugin of plugins) {
    for (const skill of plugin.skills) {
      set.add(typeof skill === 'string' ? skill : skill.name);
    }
  }
  return set;
}

/** 判定一个技能是否"已安装"——装了某个插件、或本地/项目技能，都算。跟 SkillPanel/index.tsx
 * 原来内联的 isSkillInstalled 同一份规则。 */
export function isSkillInstalled<T extends { name: string; source?: string; installed?: boolean }>(
  skill: T,
  installedSkillNames: Set<string>,
): boolean {
  return installedSkillNames.has(skill.name) || skill.installed === true || skill.source === 'local' || skill.source === 'project';
}

/** computeMySkills 之后，默认只保留"已启用"的技能——跟 SkillPanel/index.tsx "我的技能" tab
 * 默认 mySkillsSubTab==='enabled' 同一口径。手动创建插件的"添加技能"弹窗（CreatePluginPage.tsx）
 * 之前没有这层过滤，会把用户已停用的技能也列出来，2026-08-25 改成共用这份规则。 */
export function filterEnabledMySkills<T extends { name: string; source?: string; enabled?: boolean; installed?: boolean }>(
  skills: T[],
  installedSkillNames: Set<string>,
): T[] {
  return skills.filter((skill) => isSkillInstalled(skill, installedSkillNames) && skill.enabled !== false);
}
