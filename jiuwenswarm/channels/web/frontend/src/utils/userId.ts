/**
 * 用户标识解析工具
 *
 * 用于在 WebSocket 连接 URL 上携带 user_id，使 gateway 能为 faas
 * 注入 X-Session-Context（CreateSandbox 绑定用户标识）。
 *
 * 来源优先级（与 TUI --user-id 语义对称，浏览器侧无法设置自定义 header，
 * 只能走 query string）：
 *   1. URL 查询参数 ?user_id=...（首次访问带入）
 *   2. localStorage（刷新 / 后续不带 query 时复用）
 *
 * 解析到非空值时回写 localStorage，保证后续访问无需再带 query。
 */

const USER_ID_STORAGE_KEY = 'jiuwenswarm:user_id';

function readFromQuery(): string {
  if (typeof window === 'undefined' || typeof window.location === 'undefined') {
    return '';
  }
  const raw = new URLSearchParams(window.location.search).get('user_id');
  if (typeof raw !== 'string') {
    return '';
  }
  return raw.trim();
}

function readFromStorage(): string {
  if (typeof window === 'undefined' || typeof window.localStorage === 'undefined') {
    return '';
  }
  try {
    const raw = window.localStorage.getItem(USER_ID_STORAGE_KEY);
    if (typeof raw !== 'string') {
      return '';
    }
    return raw.trim();
  } catch {
    // localStorage 不可用（隐私模式等）时静默降级
    return '';
  }
}

function writeToStorage(value: string): void {
  if (typeof window === 'undefined' || typeof window.localStorage === 'undefined') {
    return;
  }
  try {
    if (value) {
      window.localStorage.setItem(USER_ID_STORAGE_KEY, value);
    } else {
      window.localStorage.removeItem(USER_ID_STORAGE_KEY);
    }
  } catch {
    // localStorage 不可用（隐私模式等）时静默降级，不影响业务连接
  }
}

export function resolveUserId(): string {
  const fromQuery = readFromQuery();
  if (fromQuery) {
    writeToStorage(fromQuery);
    return fromQuery;
  }
  return readFromStorage();
}
