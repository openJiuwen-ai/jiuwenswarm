const avatarColors = [
  "bg-red-500",
  "bg-orange-500",
  "bg-amber-500",
  "bg-yellow-500",
  "bg-lime-500",
  "bg-green-500",
  "bg-emerald-500",
  "bg-teal-500",
  "bg-cyan-500",
  "bg-sky-500",
  "bg-blue-500",
  "bg-indigo-500",
  "bg-violet-500",
  "bg-purple-500",
  "bg-fuchsia-500",
  "bg-pink-500",
  "bg-rose-500",
];

/** 技能头像：展示字母大写；颜色按首字母小写哈希，避免 Weather/weather 颜色不一致。 */
export function getSkillAvatar(name: string): { firstChar: string; color: string } {
  const trimmed = String(name || "").trim() || "?";
  const firstChar = trimmed.charAt(0).toUpperCase();
  const colorSeed = trimmed.charAt(0).toLowerCase().charCodeAt(0) || 0;
  const colorIndex = colorSeed % avatarColors.length;
  return { firstChar, color: avatarColors[colorIndex] };
}
