/** 技能展示名：广场中文名 > display_name > 包内 slug（无 market 时与改前一致） */
export function skillPresentationName(item: {
  name: string;
  market_display_name?: string | null;
  display_name?: string | null;
}): string {
  const market = String(item.market_display_name || '').trim();
  const display = String(item.display_name || '').trim();
  const name = String(item.name || '').trim();
  return market || display || name;
}
