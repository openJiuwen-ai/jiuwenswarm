import type { Plugin } from 'vite'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import svgr from 'vite-plugin-svgr'
import { createHash } from 'node:crypto'
import path from 'path'
import fs from 'fs'

type ConfigWithLogger = { logger?: { error?: (msg: string, opts?: { error?: Error }) => void } }

interface ErrorWithCode {
  code?: string
}

/**
 * 敏感字段键名判断：与后端 jiuwenswarm.common.utils._KV_SENSITIVE_PATTERN +
 * _NAMED_SENSITIVE_KV_PATTERN 的并集语义保持一致。
 *
 * 后端用通用单词边界 ``(?<![A-Za-z0-9])...(?![A-Za-z0-9])``，可正确匹配连字符/
 * 点号等分隔的键名（``my-api-key`` / ``my.token``）。这里采用与之等价的 token 化
 * 方案：将键名按非字母数字切分，若 token 集合命中敏感词即判定为敏感键。该方案
 * 与后端 ``stream_logger._looks_secret`` 思路一致，天然覆盖各种分隔符，且能排除
 * ``context_window_tokens``（``tokens`` 复数 = 计数，非凭证）。
 */
const SECRET_TOKENS = new Set([
  'token', 'password', 'passwd', 'pwd', 'secret', 'apikey', 'authorization',
  'authorisation', 'credential', 'userid',
])
// 显式排除的非凭证键名（含敏感子串但语义非凭证）。
const NON_SENSITIVE_KEY_OVERRIDES = new Set(['context_window_tokens', 'context_window_token'])

function looksSecretKey(keyLower: string): boolean {
  if (!keyLower || NON_SENSITIVE_KEY_OVERRIDES.has(keyLower)) return false
  // 按非字母数字切分（与后端 _looks_secret 一致：_ - . / 等都是分隔符）。
  const tokens = new Set(
    keyLower.split(/[^a-z0-9]+/i).filter((t) => t.length > 0)
  )
  if (tokens.size === 0) return false
  // "tokens" 复数 = 计数字段（tokens_used / total_tokens），非凭证，排除。
  if (tokens.has('tokens')) return false
  if (setIntersect(tokens, SECRET_TOKENS)) return true
  // api_key / api-key → {api, key}；private_key → {private, key}；
  // access_token → {access, token}（token 已覆盖，但显式列出双 token 防漏）；
  // user_id → {user, id}；refresh_token → token 已覆盖。
  if (tokens.has('api') && tokens.has('key')) return true
  if (tokens.has('private') && tokens.has('key')) return true
  if (tokens.has('user') && tokens.has('id')) return true
  return false
}

function setIntersect(a: Set<string>, b: Set<string>): boolean {
  // 用 Array.from 规避 Set 直接迭代在某些 TS target 下的 TS2802。
  for (const x of Array.from(a)) if (b.has(x)) return true
  return false
}

/**
 * 凭证值形态：即便没有敏感键名上下文，值本身是已知前缀的凭证（OpenAI/Bearer/JWT/
 * GitHub/GitLab token）也要脱敏。
 *
 * Bearer 用后行断言只捕获令牌值本体（不含 "Bearer " 前缀），便于跨端指纹关联。
 */
const SENSITIVE_VALUE_PATTERNS: { re: RegExp }[] = [
  { re: /\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g }, // JWT
  { re: /\bsk-[A-Za-z0-9]{8,}\b/g },                                 // OpenAI 风格
  { re: /\bghp_[A-Za-z0-9]{20,}\b/g },                               // GitHub PAT
  { re: /\bglpat-[A-Za-z0-9_-]{20,}\b/g },                           // GitLab PAT
  { re: /(?<=\bBearer\s+)[A-Za-z0-9\-._~+/]+=*/gi },                // Authorization Bearer（仅 token 本体）
]

/**
 * 对单个敏感值做带指纹的脱敏：``******(fp:xxxxxxxx)``。
 * 指纹 = SHA256(值) 前 4 字节（8 位 hex），与后端 fingerprint 算法一致，
 * 同一 key 在前后端两套日志中指纹相同，便于跨端关联排查。不可逆。
 *
 * 若 value 本身已是脱敏产物（``******`` 或 ``******(fp:..)``），原样返回不重算，
 * 与后端 masked_with_fp / is_already_masked 判断一致——避免对"指纹值"再算
 * 指纹导致跨日志关联失效。
 */
const ALREADY_MASKED_RE = new RegExp(
  '^' + '******'.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(\\(fp:[0-9a-f]{8}\\))?$'
)

function isAlreadyMasked(value: string): boolean {
  return !!value && ALREADY_MASKED_RE.test(value)
}

function maskWithFp(value: string): string {
  if (!value) return '******'
  if (isAlreadyMasked(value)) return value
  try {
    const fp = createHash('sha256').update(value, 'utf8').digest('hex').slice(0, 8)
    return `******(fp:${fp})`
  } catch {
    return '******'
  }
}

/**
 * 对值做形态脱敏：把值中出现的凭证片段（sk-/Bearer/JWT 等）原地替换为带指纹掩码。
 * 用于无敏感键名但值含凭证的场景（如一段日志文本里夹带 sk-xxx）。
 */
function maskValueShapes(value: string): string {
  let out = value
  for (const { re } of SENSITIVE_VALUE_PATTERNS) {
    out = out.replace(re, (m) => maskWithFp(m))
  }
  return out
}

/**
 * 递归脱敏任意结构（对象/数组/字符串）。键名命中敏感词的值整体替换为 ``******(fp:..)``；
 * 字符串值再做形态脱敏兜底。与后端 SensitiveDataFilter 行为对齐。
 */
function maskSensitive(payload: unknown): unknown {
  if (payload === null || payload === undefined) return payload
  if (Array.isArray(payload)) {
    return payload.map((item) => maskSensitive(item))
  }
  if (typeof payload === 'object') {
    const result: Record<string, unknown> = {}
    for (const [k, v] of Object.entries(payload as Record<string, unknown>)) {
      if (looksSecretKey(k.toLowerCase())) {
        // 敏感键：整体脱敏（保留指纹）。非字符串值先序列化再算指纹，便于关联。
        const strVal = typeof v === 'string' ? v : safeStringify(v)
        result[k] = maskWithFp(strVal)
      } else {
        result[k] = maskSensitive(v)
      }
    }
    return result
  }
  if (typeof payload === 'string') {
    return maskValueShapes(payload)
  }
  return payload
}

function safeStringify(v: unknown): string {
  try {
    return typeof v === 'string' ? v : JSON.stringify(v)
  } catch {
    return String(v)
  }
}


/**
 * file-api 使用的项目根目录，需与后端 get_root_dir() 一致，前端编辑的 HEARTBEAT.md 才会被心跳读到。
 * 优先级：环境变量 > 已存在的用户工作区 ~/.jiuwenswarm > 仓库根。
 */
function resolveProjectRootDir(): string {
  const envRoot = process.env.JIUWENSWARM_ROOT || process.env.JIUWENSWARM_PROJECT_ROOT
  if (envRoot) {
    const resolved = path.resolve(envRoot)
    console.log('[file-api] 使用环境变量根目录:', resolved)
    return resolved
  }
  const home = process.env.USERPROFILE || process.env.HOME || ''
  if (home) {
    // 优先检查多实例环境变量
    const envWorkspace = process.env.JIUWENSWARM_DATA_DIR
    if (envWorkspace) {
      console.log('[file-api] 使用 JIUWENSWARM_DATA_DIR:', path.resolve(envWorkspace))
      return path.resolve(envWorkspace)
    }
    const userWorkspace = path.join(home, '.jiuwenswarm')
    if (fs.existsSync(userWorkspace)) {
      console.log('[file-api] 使用用户工作区:', path.resolve(userWorkspace))
      return path.resolve(userWorkspace)
    }
  }
  const repoRoot = path.resolve(__dirname, '../../../')
  console.log('[file-api] 使用仓库根目录:', repoRoot)
  return repoRoot
}

/** WS proxy 中常见的、可安全忽略的 socket 错误码（跨平台） */
const WS_PROXY_IGNORABLE_CODES = new Set([
  'EPIPE',          // 对端已关闭
  'ECONNRESET',     // 连接被重置
  'ECONNABORTED',   // 连接被中止 (Windows 常见)
  'ECONNREFUSED',   // 后端未启动 / 端口不可达
  'ERR_STREAM_WRITE_AFTER_END',
])

/** 过滤 Vite 内置的 ws proxy socket 报错，避免控制台刷屏 */
function suppressWsProxySocketErrors(): Plugin {
  return {
    name: 'suppress-ws-proxy-socket-errors',
    config(config) {
      const logger = (config as ConfigWithLogger).logger
      if (!logger?.error) return
      const orig = logger.error.bind(logger)
      logger.error = (msg: string, opts?: unknown) => {
        if (typeof msg === 'string' && msg.includes('ws proxy socket error')) {
          const code = (opts as { error?: ErrorWithCode } | undefined)?.error?.code
          if (code && WS_PROXY_IGNORABLE_CODES.has(code)) return
        }
        orig(msg, opts as { error?: Error } | undefined)
      }
    },
  }
}

/** 在 dev 模式下将前端上报的 /ws req/res/event 记录到本地文件 */
function devWsTrafficLogger(): Plugin {
  return {
    name: 'dev-ws-traffic-logger',
    configureServer(server) {
      const projectRootDir = resolveProjectRootDir()
      const agentDir = path.resolve(projectRootDir, 'agent')
      const logDir = path.resolve(agentDir, '.logs')
      const logFile = path.resolve(logDir, 'ws-dev.log')
      fs.mkdirSync(logDir, { recursive: true })
      // 每次前端 dev 服务启动时清空日志，避免历史数据干扰排查。
      fs.writeFileSync(logFile, '', 'utf8')

      server.middlewares.use('/__dev/ws-log', (req, res) => {
        if (req.method === 'GET') {
          const url = new URL(req.url || '/__dev/ws-log', 'http://localhost')
          const limitRaw = Number(url.searchParams.get('limit') || '300')
          const limit = Number.isFinite(limitRaw) ? Math.max(1, Math.min(2000, Math.floor(limitRaw))) : 300
          fs.readFile(logFile, 'utf8', (error, content) => {
            if (error) {
              const code = (error as NodeJS.ErrnoException).code
              if (code === 'ENOENT') {
                res.statusCode = 200
                res.setHeader('content-type', 'application/json; charset=utf-8')
                res.end(JSON.stringify({ ok: true, entries: [], count: 0 }))
                return
              }
              server.config.logger.error(`[dev-ws-logger] read failed: ${error.message}`)
              res.statusCode = 500
              res.setHeader('content-type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ ok: false, error: 'read_failed' }))
              return
            }
            const lines = content
              .split('\n')
              .map((line) => line.trim())
              .filter(Boolean)
              .slice(-limit)
            const entries = lines.map((line) => {
              try {
                return JSON.parse(line)
              } catch {
                return line
              }
            })
            res.statusCode = 200
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ ok: true, entries, count: entries.length }))
          })
          return
        }

        if (req.method !== 'POST') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ ok: false, error: 'method_not_allowed' }))
          return
        }

        let raw = ''
        req.on('data', (chunk) => {
          raw += chunk.toString()
        })
        req.on('end', () => {
          const now = new Date().toISOString()
          let payload: unknown = raw
          if (raw) {
            try {
              payload = JSON.parse(raw)
            } catch {
              payload = raw
            }
          }
          // 写盘前脱敏：前端会把 config.get/config.validate_model 等报文（含
          // api_key/token/secret）原样上报给 vite dev server，vite 再 appendFile
          // 写进 ws-dev.log。此处对 payload 递归脱敏，避免 api_key 明文落盘。
          // 与后端 SensitiveDataFilter 行为/指纹算法一致，便于跨端关联排查。
          const maskedPayload = maskSensitive(payload)
          const line = `${JSON.stringify({ ts: now, payload: maskedPayload })}\n`
          fs.appendFile(logFile, line, (error) => {
            if (error) {
              server.config.logger.error(`[dev-ws-logger] write failed: ${error.message}`)
              res.statusCode = 500
              res.setHeader('content-type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ ok: false, error: 'write_failed' }))
              return
            }
            res.statusCode = 200
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ ok: true }))
          })
        })
      })
    },
  }
}

/** file/share HTTP 已迁 Gateway Web HTTP；dev 经 proxy 转发（见 server.proxy）。 */

// https://vitejs.dev/config/
function portFromEnv(name: string, fallback: number): number {
  const value = Number.parseInt(process.env[name] ?? '', 10)
  return Number.isInteger(value) && value > 0 && value <= 65535 ? value : fallback
}

function resolveWebHttpPort(wsPort: number): number {
  const envPort = portFromEnv('GATEWAY_WEB_HTTP_PORT', 0)
  let port = envPort || wsPort + 2
  const gatewayPort = portFromEnv('GATEWAY_PORT', wsPort + 1)
  if (port === wsPort || port === gatewayPort) {
    port = Math.max(wsPort, gatewayPort) + 1
  }
  return port
}

function parseLoginAuthSimulate(raw: string | undefined): boolean {
  const value = (raw ?? '').trim().toLowerCase()
  if (!value) return true
  if (value === 'true') return true
  if (value === 'false') return false
  throw new Error(
    `LOGIN_AUTH_SIMULATE 配置非法：仅支持 true 或 false，当前值为 ${JSON.stringify(raw)}`
  )
}

/** Mirrors ``jiuwenswarm.common.local_env_config.is_enterprise`` for Vite startup checks. */
function isEnterpriseEdition(): boolean {
  const edition = (process.env.JIUWENSWARM_EDITION ?? process.env.VITE_JIUWENSWARM_EDITION ?? '')
    .trim()
    .toLowerCase()
  return edition === 'enterprise'
}

function loginAuthStartupCheck(): Plugin {
  const enterprise = isEnterpriseEdition()
  const simulateRaw = process.env.LOGIN_AUTH_SIMULATE ?? process.env.VITE_LOGIN_AUTH_SIMULATE
  const simulate = parseLoginAuthSimulate(simulateRaw)

  return {
    name: 'login-auth-startup-check',
    async configureServer() {
      if (simulateRaw === undefined || !simulateRaw.trim()) {
        console.info(
          '[jiuwenswarm-web] LOGIN_AUTH_SIMULATE 未配置，按默认值 true 启用登录认证模拟调试'
        )
      }
      if (!enterprise) {
        console.info('[jiuwenswarm-web] 单机版模式：跳过企业登录认证')
        if (!simulate) {
          console.warn(
            '[jiuwenswarm-web] 配置冲突：personal 模式仍将跳过企业登录；' +
            'LOGIN_AUTH_SIMULATE=false 不会启用正式身份认证'
          )
        }
        return
      }
      if (simulate) {
        console.info('[jiuwenswarm-web] 【登录认证模拟调试模式已开启】不调用客户侧 manager ID认证服务')
        return
      }

      console.info('[jiuwenswarm-web] 【正式身份认证模式，依赖manager ID认证服务】')
      const targets = [
        { name: 'manager ID认证服务', env: 'USER_WEB_IDP_TARGET', target: process.env.USER_WEB_IDP_TARGET, path: '/v1/auth/me' },
        { name: 'Manager业务接口', env: 'USER_WEB_MANAGER_TARGET', target: process.env.USER_WEB_MANAGER_TARGET, path: '/api/v1/user-console/agent-contexts' },
      ]
      const missing = targets.filter(({ target }) => !target).map(({ env }) => env)
      if (missing.length > 0) {
        console.error(
          `[jiuwenswarm-web] 正式登录模式下未配置 ${missing.join('、')}；` +
          '请配置 manager 认证及业务接口地址'
        )
        return
      }
      for (const item of targets) {
        const target = item.target!.replace(/\/$/, '')
        try {
          const response = await fetch(`${target}${item.path}`, {
            signal: AbortSignal.timeout(3000),
          })
          if (response.status >= 500) throw new Error(`HTTP ${response.status}`)
          console.info(`[jiuwenswarm-web] ${item.name}连通性检查通过：${target}`)
        } catch (error) {
          console.error(
            `[jiuwenswarm-web] 当前为正式登录模式，${item.name}暂不可用；` +
            `请检查 ${item.env}（${target}）：${error instanceof Error ? error.message : String(error)}`
          )
        }
      }
    },
  }
}

const frontendPort = portFromEnv('FRONTEND_PORT', 5173)
const webPort = portFromEnv('WEB_PORT', 19000)
const webTarget =
  process.env.GATEWAY_WEB_WS_URL?.replace(/\/$/, '') ||
  process.env.GATEWAY_URL?.replace(/\/$/, '') ||
  `http://127.0.0.1:${webPort}`
const webHttpPort = resolveWebHttpPort(webPort)
const webHttpTarget =
  process.env.GATEWAY_WEB_HTTP_URL?.replace(/\/$/, '') ||
  `http://127.0.0.1:${webHttpPort}`

export default defineConfig({
  // 相对资源路径同时支持独立根路径与 Manager Web 的 /chat/ 同源转发。
  base: './',
  plugins: [loginAuthStartupCheck(), suppressWsProxySocketErrors(), devWsTrafficLogger(), react(), svgr()],
  optimizeDeps: {
    include: ['exceljs', 'jszip', 'saxes', 'ssf'],
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      'virtual:login-auth-simulate-provider': path.resolve(
        __dirname,
        './src/auth/simulate/available.ts',
      ),
    },
  },
  server: {
    host: '0.0.0.0',
    port: frontendPort,
    strictPort: true,
    // Manager Web 企业链路把 /chat 反代到 http://jiuwenclaw-web:5173，
    // Vite 5.4+ 默认拒绝未登记 Host，会直接 403。
    allowedHosts: true,
    proxy: {
      '/idp': { target: process.env.USER_WEB_IDP_TARGET || 'http://127.0.0.1:8770', changeOrigin: true, rewrite: (p) => p.replace(/^\/idp/, '') },
      '/manager-api': { target: process.env.USER_WEB_MANAGER_TARGET || 'http://127.0.0.1:8765', changeOrigin: true, rewrite: (p) => p.replace(/^\/manager-api/, '/api') },
      '/file-api': {
        target: webHttpTarget,
        changeOrigin: true,
      },
      '/share-api': {
        target: webHttpTarget,
        changeOrigin: true,
      },
      // More specific than '/api' — enterprise HTTP APIs live on Web HTTP, not WS port.
      '/api/v1': {
        target: webHttpTarget,
        changeOrigin: true,
      },
      '/gateway-api': {
        target: webHttpTarget,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/gateway-api/, '/api'),
      },
      '/api/sessions': {
        target: webHttpTarget,
        changeOrigin: true,
      },
      '/api': {
        target: webTarget,
        changeOrigin: true,
      },
      '/ws': {
        target: webTarget,
        ws: true,
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on('error', (err, _req, _res) => {
            const code = (err as ErrorWithCode).code
            if (code && WS_PROXY_IGNORABLE_CODES.has(code)) {
              return
            }
            console.error('[vite] ws proxy error:', err.message)
          })
        },
      },
    },
  },
})
