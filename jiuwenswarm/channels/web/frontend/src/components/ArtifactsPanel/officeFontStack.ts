const MICROSOFT_YAHEI = 'Microsoft YaHei';
const SIMSUN = 'SimSun';
const CROSS_PLATFORM_FALLBACK_FONTS = [
  'PingFang SC',
  'HarmonyOS Sans SC',
  'HarmonyOS Sans',
  'Noto Sans CJK SC',
  'Noto Sans SC',
  'Source Han Sans SC',
  'WenQuanYi Micro Hei',
] as const;

export function officeFontStack(primaryFont: string | undefined, ...documentFonts: Array<string | undefined>): string {
  const secondaryFonts = documentFonts.map(normalizeFontName).filter((font): font is string => Boolean(font));

  // 微软雅黑优先于宋体（屏幕预览更清晰）：文档含宋体就插到宋体前，否则追加末尾兜底
  const simSunIndex = secondaryFonts.findIndex(isSimSun);
  if (simSunIndex >= 0) secondaryFonts.splice(simSunIndex, 0, MICROSOFT_YAHEI);
  else secondaryFonts.push(MICROSOFT_YAHEI);

  const declared = [normalizeFontName(primaryFont), ...secondaryFonts, ...CROSS_PLATFORM_FALLBACK_FONTS];
  // 文档指定宋体时确保宋体仍在栈中（此时微软雅黑已插到其前）
  if (!declared.some(font => font !== undefined && isSimSun(font))) declared.push(SIMSUN);

  const seen = new Set<string>();
  const unique = declared.filter((font): font is string => {
    if (!font) return false;
    const key = font.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return [...unique.map(quoteCssString), 'sans-serif'].join(', ');
}

function normalizeFontName(font: string | undefined): string | undefined {
  return font?.trim() || undefined;
}

function isSimSun(font: string): boolean {
  return font.toLowerCase() === 'simsun' || font === '宋体';
}

function quoteCssString(value: string): string {
  let escaped = '';
  for (const character of value) {
    const codePoint = character.codePointAt(0) ?? 0;
    if (codePoint === 0) escaped += '\uFFFD';
    else if ((codePoint >= 1 && codePoint <= 0x1f) || codePoint === 0x7f) escaped += `\\${codePoint.toString(16)} `;
    else if (character === '"' || character === '\\') escaped += `\\${character}`;
    else escaped += character;
  }
  return `"${escaped}"`;
}
