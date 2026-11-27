/**
 * 将 SkillNet / GitHub 技能目录 URL 规范化为可比较形式（主机小写、去尾斜杠等）。
 * 用于搜索结果 skill_url 与本地 skills[].origin 对照。
 */
export function normalizeSkillNetUrl(raw: string): string {
  const s = raw.trim();
  if (!s) return "";
  try {
    const u = new URL(s.startsWith("http://") || s.startsWith("https://") ? s : `https://${s}`);
    if (u.hostname.toLowerCase() === "github.com") {
      u.protocol = "https:";
    }
    u.hostname = u.hostname.toLowerCase();
    let path = u.pathname;
    if (path.length > 1 && path.endsWith("/")) {
      path = path.slice(0, -1);
    }
    return `${u.origin}${path}${u.search}${u.hash}`;
  } catch {
    return s.replace(/\/$/, "").toLowerCase();
  }
}
