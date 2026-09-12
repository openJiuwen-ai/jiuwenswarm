import type { Plugin } from 'vite'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import svgr from 'vite-plugin-svgr'
import { spawnSync } from 'child_process'
import { createHash } from 'node:crypto'
import { createHmac, timingSafeEqual } from 'node:crypto'
import https from 'node:https'
import type { ServerResponse } from 'http'
import path from 'path'
import fs from 'fs'
import os from 'os'

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
 * GitHub/GitLab token）也要脱敏。与后端 _SENSITIVE_PATTERNS 对齐。
 *
 * Bearer 用后行断言只捕获令牌值本体（不含 "Bearer " 前缀），使指纹与后端
 * _BEARER_SENSITIVE_PATTERN 的 group(2)（token 本体）一致，跨端可关联。
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
 * 指纹 = SHA256(值) 前 4 字节（8 位 hex），与后端 _fingerprint 算法一致，
 * 同一 key 在前后端两套日志中指纹相同，便于跨端关联排查。不可逆。
 *
 * 若 value 本身已是脱敏产物（``******`` 或 ``******(fp:..)``），原样返回不重算，
 * 与后端 _masked_with_fp 的 _is_already_masked 判断一致——避免对"指纹值"再算
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
 * file-api 使用的项目根目录，需与后端 get_root_dir() 一致。
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

const FILE_CONTENT_ENCODING_ALIASES: Record<string, string> = {
  utf8: 'utf-8',
  'utf_8': 'utf-8',
  gb2312: 'gb18030',
  gbk: 'gb18030',
  'shift-jis': 'shift_jis',
  sjis: 'shift_jis',
  euc_kr: 'euc-kr',
  latin1: 'iso-8859-1',
}

function normalizeFileContentEncoding(encoding: string): string {
  const key = encoding.trim().toLowerCase()
  return FILE_CONTENT_ENCODING_ALIASES[key] ?? key
}

function decodeFileContent(raw: Buffer, requestedEncoding: string): { content: string; encoding: string } {
  const normalizedEncoding = normalizeFileContentEncoding(requestedEncoding || 'utf-8')
  if (normalizedEncoding !== 'auto') {
    return {
      content: new TextDecoder(normalizedEncoding, { fatal: true }).decode(raw),
      encoding: normalizedEncoding,
    }
  }

  const candidates = ['utf-8', 'gb18030', 'big5', 'shift_jis', 'euc-kr', 'iso-8859-1']
  for (const candidate of candidates) {
    try {
      return {
        content: new TextDecoder(candidate, { fatal: true }).decode(raw),
        encoding: candidate,
      }
    } catch {
      /* try next encoding */
    }
  }
  throw new Error('Unable to decode file with any known encoding')
}

const DOWNLOAD_CONTENT_TYPES: Record<string, string> = {
  '.md': 'text/markdown; charset=utf-8',
  '.markdown': 'text/markdown; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.jsonl': 'application/x-ndjson; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.html': 'text/html; charset=utf-8',
  '.htm': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.ts': 'text/plain; charset=utf-8',
  '.tsx': 'text/plain; charset=utf-8',
  '.py': 'text/plain; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.svg': 'image/svg+xml',
  '.bmp': 'image/bmp',
  '.avif': 'image/avif',
  '.pdf': 'application/pdf',
  '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

function downloadContentType(filePath: string): string {
  return DOWNLOAD_CONTENT_TYPES[path.extname(filePath).toLowerCase()] || 'application/octet-stream'
}

function handleFileStreamError(res: ServerResponse, error: NodeJS.ErrnoException): void {
  if (res.headersSent) {
    res.destroy(error)
    return
  }

  res.statusCode = error.code === 'EACCES' || error.code === 'EPERM' ? 403 : 500
  res.removeHeader('content-length')
  res.removeHeader('content-disposition')
  res.removeHeader('accept-ranges')
  res.removeHeader('content-range')
  res.setHeader('content-type', 'application/json; charset=utf-8')
  res.end(JSON.stringify({
    error: res.statusCode === 403 ? 'file_access_denied' : 'file_read_failed',
  }))
}

function resolveFileDownloadSecret(): string | null {
  const envSecret = process.env.JIUWENSWARM_FILE_DOWNLOAD_SECRET
  if (envSecret && envSecret.length >= 32) return envSecret

  const workspace = process.env.JIUWENSWARM_WORKSPACE || path.join(process.env.HOME || process.env.USERPROFILE || '', '.jiuwenswarm')
  const secretPath = path.join(workspace, 'config', '.file_download_secret')
  try {
    const secret = fs.readFileSync(secretPath, 'utf8').trim()
    return secret.length >= 32 ? secret : null
  } catch {
    return null
  }
}

function validateFileDownloadToken(token: string): { path: string } | null {
  const parts = token.split('.')
  if (parts.length !== 2) return null
  const [payloadBase64, signature] = parts

  const secret = resolveFileDownloadSecret()
  if (!secret) return null
  const expected = createHmac('sha256', secret).update(payloadBase64).digest('hex')
  const actual = Buffer.from(signature, 'hex')
  const expectedBuffer = Buffer.from(expected, 'hex')
  if (actual.length !== expectedBuffer.length || !timingSafeEqual(actual, expectedBuffer)) return null

  try {
    const payload = JSON.parse(Buffer.from(payloadBase64, 'base64url').toString('utf8')) as Record<string, unknown>
    // 普通 file download token：携带 path 字段
    if (typeof payload.path === 'string' && payload.path && typeof payload.sid === 'string') {
      return { path: payload.path }
    }
    // skill_content_image token：携带 name + relative_path，需解析为绝对路径
    if (String(payload.purpose || '').trim() === 'skill_content_image') {
      const name = String(payload.name || '').trim()
      const relativePath = String(payload.relative_path || '').trim()
      if (!name || !relativePath) return null
      const rootDir = resolveProjectRootDir()
      const skillDir = path.resolve(rootDir, 'agent', 'workspace', 'skills', name)
      const fullPath = path.resolve(skillDir, relativePath)
      // 安全检查：确保路径在 skills 目录下
      const skillsRoot = path.resolve(rootDir, 'agent', 'workspace', 'skills')
      const rel = path.relative(skillsRoot, fullPath)
      if (rel.startsWith('..') || path.isAbsolute(rel)) return null
      return { path: fullPath }
    }
    return null
  } catch {
    return null
  }
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

/** 将文件读取接口挂到 Vite dev server，避免额外占用 3003 端口 */
function devFileContentApi(): Plugin {
  const projectRootDir = resolveProjectRootDir()
  const workspaceRootDir = path.resolve(projectRootDir, 'agent')
  const sessionsRootDir = path.resolve(workspaceRootDir, 'sessions')
  const agentTeamsRootDir = path.resolve(projectRootDir, '.agent_teams')
  const webLogsRootDir = path.resolve(workspaceRootDir, '.logs')
  const autoHarnessDir = path.resolve(projectRootDir, 'auto-harness')
  const generateAgentFoldersScriptPath = path.resolve(__dirname, '../../../scripts/generate-agent-folders.js')
  // dev 模式默认开启调试视图，与“前端 dev 即调试模式”一致。
  let wsDisableCompress = true
  const isMarkdownFile = (targetPath: string) => {
    const ext = path.extname(targetPath).toLowerCase()
    return ext === '.md' || ext === '.mdx'
  }
  const isPathUnderAllowedRoot = (targetPath: string) => {
    const relativeWorkspacePath = path.relative(workspaceRootDir, targetPath)
    const inWorkspace = !relativeWorkspacePath.startsWith('..') && !path.isAbsolute(relativeWorkspacePath)
    const relativeAgentTeamsPath = path.relative(agentTeamsRootDir, targetPath)
    const inAgentTeams = !relativeAgentTeamsPath.startsWith('..') && !path.isAbsolute(relativeAgentTeamsPath)
    const relativeLogsPath = path.relative(webLogsRootDir, targetPath)
    const inWebLogs = !relativeLogsPath.startsWith('..') && !path.isAbsolute(relativeLogsPath)
    const relativeAutoHarnessPath = path.relative(autoHarnessDir, targetPath)
    const inAutoHarness = !relativeAutoHarnessPath.startsWith('..') && !path.isAbsolute(relativeAutoHarnessPath)
    return inWorkspace || inAgentTeams || inWebLogs || inAutoHarness
  }

  return {
    name: 'dev-file-content-api',
    configureServer(server) {
      // GitCode API 代理（手动实现，支持 GET/POST）
      server.middlewares.use('/gitcode-api', (req, res) => {
        const proxyPath = (req.url || '').replace(/^\/gitcode-api/, '');
        const chunks: Buffer[] = [];
        req.on('data', (chunk: Buffer) => chunks.push(chunk));
        req.on('end', () => {
          const body = Buffer.concat(chunks);
          const proxyReq = https.request({
            method: req.method,
            hostname: 'gitcode.com',
            path: proxyPath,
            headers: { ...req.headers, host: 'gitcode.com' },
          }, (proxyRes) => {
            res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
            proxyRes.pipe(res);
          });
          proxyReq.on('error', (err: Error) => {
            console.error('[vite] gitcode-api proxy error:', err.message);
            res.writeHead(502, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ error: err.message }));
          });
          if (body.length > 0) proxyReq.write(body);
          proxyReq.end();
        });
      });

      // GitHub OAuth token 兑换代理 → github.com（支持 GET/POST）
      // 用于 POST /login/oauth/access_token（code → access_token）
      server.middlewares.use('/github-oauth', (req, res) => {
        const proxyPath = (req.url || '').replace(/^\/github-oauth/, '');
        const chunks: Buffer[] = [];
        req.on('data', (chunk: Buffer) => chunks.push(chunk));
        req.on('end', () => {
          const body = Buffer.concat(chunks);
          const proxyReq = https.request({
            method: req.method,
            hostname: 'github.com',
            path: proxyPath,
            headers: {
              ...req.headers,
              host: 'github.com',
              accept: 'application/json',
            },
          }, (proxyRes) => {
            res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
            proxyRes.pipe(res);
          });
          proxyReq.on('error', (err: Error) => {
            console.error('[vite] github-oauth proxy error:', err.message);
            res.writeHead(502, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ error: err.message }));
          });
          if (body.length > 0) proxyReq.write(body);
          proxyReq.end();
        });
      });

      // GitHub API 代理 → api.github.com（支持 GET/POST）
      // 用于 GET /user（access_token → 用户信息）
      server.middlewares.use('/github-api', (req, res) => {
        const proxyPath = (req.url || '').replace(/^\/github-api/, '');
        const chunks: Buffer[] = [];
        req.on('data', (chunk: Buffer) => chunks.push(chunk));
        req.on('end', () => {
          const body = Buffer.concat(chunks);
          const proxyReq = https.request({
            method: req.method,
            hostname: 'api.github.com',
            path: proxyPath,
            headers: {
              ...req.headers,
              host: 'api.github.com',
              accept: 'application/json',
              'user-agent': 'jiuwenswarm-web',
            },
          }, (proxyRes) => {
            res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
            proxyRes.pipe(res);
          });
          proxyReq.on('error', (err: Error) => {
            console.error('[vite] github-api proxy error:', err.message);
            res.writeHead(502, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ error: err.message }));
          });
          if (body.length > 0) proxyReq.write(body);
          proxyReq.end();
        });
      });
      server.middlewares.use('/share-api/snapshot', (req, res) => {
        const writeJson = (statusCode: number, payload: unknown) => {
          res.statusCode = statusCode
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify(payload))
        }

        if (req.method !== 'GET' && req.method !== 'HEAD') {
          writeJson(405, { error: 'method_not_allowed' })
          return
        }
        const url = new URL(req.url || '/share-api/snapshot', 'http://localhost')
        const sessionId = (url.searchParams.get('session_id') || '').trim()
        if (!sessionId) {
          writeJson(400, { error: 'missing_session_id' })
          return
        }

        const sessionDir = path.resolve(sessionsRootDir, sessionId)
        const relativeSessionPath = path.relative(sessionsRootDir, sessionDir)
        if (relativeSessionPath.startsWith('..') || path.isAbsolute(relativeSessionPath)) {
          writeJson(404, { error: 'history_not_found' })
          return
        }

        const jsonlHistoryPath = path.resolve(sessionDir, 'history.jsonl')
        const legacyHistoryPath = path.resolve(sessionDir, 'history.json')
        const historyPath = fs.existsSync(jsonlHistoryPath) ? jsonlHistoryPath : legacyHistoryPath
        if (!fs.existsSync(sessionDir) || !fs.existsSync(historyPath)) {
          writeJson(404, { error: 'history_not_found' })
          return
        }

        try {
          const historyText = fs.readFileSync(historyPath, 'utf-8')
          const historyRaw = historyPath.endsWith('.jsonl')
            ? historyText
                .split(/\r?\n/)
                .map((line) => line.trim())
                .filter(Boolean)
                .map((line) => JSON.parse(line) as unknown)
            : (JSON.parse(historyText) as unknown)
          if (!Array.isArray(historyRaw)) {
            writeJson(400, { error: 'invalid_history_shape' })
            return
          }

          let title = path.basename(sessionDir)
          const metadataPath = path.resolve(sessionDir, 'metadata.json')
          if (fs.existsSync(metadataPath)) {
            const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf-8')) as { title?: unknown }
            if (typeof metadata.title === 'string' && metadata.title.trim()) {
              title = metadata.title.trim()
            }
          }
          if (title === path.basename(sessionDir)) {
            for (const record of historyRaw) {
              if (!record || typeof record !== 'object') continue
              const item = record as { role?: unknown; content?: unknown }
              if (item.role === 'user' && typeof item.content === 'string' && item.content.trim()) {
                title = item.content.trim().replace(/\n/g, ' ').slice(0, 80)
                break
              }
            }
          }

          const now = new Date()
          const filename = `jiuwenswarm-share-${now.toISOString().replace(/[-:]/g, '').replace(/\..+$/, '').replace('T', '-')}.png`
          const snapshot = {
            session_id: sessionId,
            metadata: {
              title,
              exported_at: now.toISOString(),
              filename,
            },
            records: historyRaw,
          }

          writeJson(200, { filename, snapshot })
        } catch (error) {
          writeJson(500, { error: 'snapshot_failed', detail: (error as Error).message })
        }
      })

      server.middlewares.use('/file-api/ws-debug-config', (req, res) => {
        if (req.method === 'GET') {
          res.statusCode = 200
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ wsDisableCompress }))
          return
        }

        if (req.method !== 'POST') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }

        let raw = ''
        req.on('data', (chunk) => {
          raw += chunk.toString()
        })
        req.on('end', () => {
          try {
            const payload = raw ? JSON.parse(raw) : {}
            if (typeof payload.wsDisableCompress !== 'boolean') {
              res.statusCode = 400
              res.setHeader('content-type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ error: 'invalid_ws_disable_compress' }))
              return
            }
            wsDisableCompress = payload.wsDisableCompress
            res.statusCode = 200
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ ok: true, wsDisableCompress }))
          } catch {
            res.statusCode = 400
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: 'invalid_json' }))
          }
        })
      })

      server.middlewares.use('/file-api/rebuild-agent-data', (_req, res) => {
        if (_req.method !== 'POST') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }

        try {
          const runResult = spawnSync(process.execPath, [generateAgentFoldersScriptPath], {
            encoding: 'utf-8',
          })
          if (runResult.status !== 0) {
            const output = `${runResult.stdout || ''}\n${runResult.stderr || ''}`.trim()
            res.statusCode = 500
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: 'rebuild_failed', detail: output || 'unknown_error' }))
            return
          }
          res.statusCode = 200
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ ok: true }))
        } catch (error) {
          res.statusCode = 500
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'rebuild_failed', detail: (error as Error).message }))
        }
      })

      server.middlewares.use('/file-api/list-markdown', (req, res) => {
        if (req.method !== 'GET') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }
        const url = new URL(req.url || '/file-api/list-markdown', 'http://localhost')
        const dir = url.searchParams.get('dir')
        if (!dir) {
          res.statusCode = 400
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'missing_dir' }))
          return
        }
        try {
          const fullDirPath = path.resolve(projectRootDir, dir)
          if (!isPathUnderAllowedRoot(fullDirPath)) {
            res.statusCode = 403
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: 'forbidden_dir' }))
            return
          }
          if (!fs.existsSync(fullDirPath) || !fs.statSync(fullDirPath).isDirectory()) {
            res.statusCode = 200
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ files: [] }))
            return
          }
          const files = fs
            .readdirSync(fullDirPath, { withFileTypes: true })
            .filter((entry) => entry.isFile())
            .map((entry) => entry.name)
            .filter((name) => isMarkdownFile(name))
            .sort((a, b) => a.localeCompare(b))
            .map((name) => ({
              name,
              path: path.relative(projectRootDir, path.resolve(fullDirPath, name)),
            }))
          res.statusCode = 200
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ files }))
        } catch (error) {
          res.statusCode = 500
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: (error as Error).message }))
        }
      })

      server.middlewares.use('/file-api/list-files', (req, res) => {
        if (req.method !== 'GET') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }
        const url = new URL(req.url || '/file-api/list-files', 'http://localhost')
        const dir = url.searchParams.get('dir')
        if (!dir) {
          res.statusCode = 400
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'missing_dir' }))
          return
        }
        try {
          const fullDirPath = path.resolve(projectRootDir, dir)
          if (!isPathUnderAllowedRoot(fullDirPath)) {
            res.statusCode = 403
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: 'forbidden_dir' }))
            return
          }
          if (!fs.existsSync(fullDirPath) || !fs.statSync(fullDirPath).isDirectory()) {
            res.statusCode = 200
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ files: [] }))
            return
          }
          const files = fs
            .readdirSync(fullDirPath, { withFileTypes: true })
            .sort((a, b) => {
              if (a.isDirectory() !== b.isDirectory()) return a.isDirectory() ? -1 : 1
              return a.name.localeCompare(b.name)
            })
            .map((entry) => {
              const absolutePath = path.resolve(fullDirPath, entry.name)
              if (entry.isDirectory()) {
                return {
                  name: entry.name,
                  path: path.relative(projectRootDir, absolutePath),
                  isMarkdown: false,
                  isDirectory: true,
                }
              }
              return {
                name: entry.name,
                path: path.relative(projectRootDir, absolutePath),
                isMarkdown: isMarkdownFile(absolutePath),
                isDirectory: false,
              }
            })
          res.statusCode = 200
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ files }))
        } catch (error) {
          res.statusCode = 500
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: (error as Error).message }))
        }
      })

      server.middlewares.use('/file-api/download', (req, res) => {
        if (req.method !== 'GET' && req.method !== 'HEAD') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }

        const url = new URL(req.url || '/file-api/download', 'http://localhost')
        const token = url.searchParams.get('token') || ''
        const payload = validateFileDownloadToken(token)
        if (!payload) {
          res.statusCode = 403
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'invalid_or_expired_token' }))
          return
        }

        let stat: fs.Stats
        try {
          stat = fs.statSync(payload.path)
        } catch {
          res.statusCode = 404
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'file_not_found' }))
          return
        }
        if (!stat.isFile()) {
          res.statusCode = 404
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'file_not_found' }))
          return
        }

        const fileSize = stat.size
        let start = 0
        let end = Math.max(0, fileSize - 1)
        let partial = false
        const range = req.headers.range
        if (range) {
          if (!range.startsWith('bytes=') || range.includes(',') || fileSize === 0) {
            res.statusCode = 416
            res.setHeader('content-range', `bytes */${fileSize}`)
            res.end()
            return
          }
          const [startText, endText] = range.slice(6).split('-', 2)
          try {
            if (startText) {
              start = Number(startText)
              end = endText ? Number(endText) : end
            } else {
              const suffixLength = Number(endText)
              if (!Number.isInteger(suffixLength) || suffixLength <= 0) throw new Error('invalid_suffix')
              start = Math.max(0, fileSize - suffixLength)
            }
            if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || start >= fileSize || end < start) throw new Error('invalid_range')
            end = Math.min(end, fileSize - 1)
            partial = true
          } catch {
            res.statusCode = 416
            res.setHeader('content-range', `bytes */${fileSize}`)
            res.end()
            return
          }
        }

        const contentLength = fileSize === 0 ? 0 : end - start + 1
        const inline = ['1', 'true'].includes((url.searchParams.get('inline') || '').toLowerCase())
        const fileName = path.basename(payload.path)
        res.statusCode = partial ? 206 : 200
        res.setHeader('content-type', downloadContentType(payload.path))
        res.setHeader('content-length', String(contentLength))
        res.setHeader('accept-ranges', 'bytes')
        res.setHeader('content-disposition', `${inline ? 'inline' : 'attachment'}; filename*=UTF-8''${encodeURIComponent(fileName)}`)
        res.setHeader('cache-control', 'no-store')
        if (partial) res.setHeader('content-range', `bytes ${start}-${end}/${fileSize}`)
        if (req.method === 'HEAD') {
          res.end()
          return
        }
        const fileStream = fs.createReadStream(payload.path, fileSize === 0 ? undefined : { start, end })
        fileStream.once('error', (error) => {
          server.config.logger.error(`[file-api] Failed to read ${payload.path}: ${(error as Error).message}`)
          handleFileStreamError(res, error)
        })
        fileStream.pipe(res)
      })

      server.middlewares.use('/file-api/raw-file', (req, res) => {
        if (req.method !== 'GET' && req.method !== 'HEAD') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }

        const url = new URL(req.url || '/file-api/raw-file', 'http://localhost')
        const filePath = url.searchParams.get('path')
        if (!filePath) {
          res.statusCode = 400
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: '缺少文件路径' }))
          return
        }

        try {
          const fullPath = path.resolve(projectRootDir, filePath)
          if (!isPathUnderAllowedRoot(fullPath)) {
            res.statusCode = 403
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: 'forbidden_path' }))
            return
          }
          if (!fs.existsSync(fullPath) || !fs.statSync(fullPath).isFile()) {
            res.statusCode = 404
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: '文件不存在', fullPath }))
            return
          }

          res.statusCode = 200
          res.setHeader('content-type', downloadContentType(fullPath))
          res.setHeader('cache-control', 'no-store')
          if (req.method === 'HEAD') {
            res.end()
            return
          }
          const fileStream = fs.createReadStream(fullPath)
          fileStream.once('error', (error) => {
            server.config.logger.error(`[file-api] Failed to read ${fullPath}: ${(error as Error).message}`)
            handleFileStreamError(res, error)
          })
          fileStream.pipe(res)
        } catch (error) {
          res.statusCode = 500
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: (error as Error).message }))
        }
      })

      // SkillHub API 代理 → teamskills.openjiuwen.com
      // 用于前端直接 POST FormData 发布技能（与 skillhub 架构对齐）
      const hubBaseUrl = process.env.VITE_HUB_API_BASE_URL || 'https://teamskills.openjiuwen.com'
      server.middlewares.use('/hub-api', (req, res) => {
        const proxyPath = (req.url || '').replace(/^\/hub-api/, '');
        const chunks: Buffer[] = [];
        req.on('data', (chunk: Buffer) => chunks.push(chunk));
        req.on('end', () => {
          const body = Buffer.concat(chunks);
          const proxyReq = https.request({
            method: req.method,
            hostname: new URL(hubBaseUrl).hostname,
            path: proxyPath,
            headers: { ...req.headers, host: new URL(hubBaseUrl).host },
          }, (proxyRes) => {
            res.writeHead(proxyRes.statusCode || 200, proxyRes.headers);
            proxyRes.pipe(res);
          });
          proxyReq.on('error', (err: Error) => {
            console.error('[vite] hub-api proxy error:', err.message);
            res.writeHead(502, { 'content-type': 'application/json' });
            res.end(JSON.stringify({ error: err.message }));
          });
          if (body.length > 0) proxyReq.write(body);
          proxyReq.end();
        });
      });

      // 技能上传：接收 multipart 文件，保存到临时目录，返回文件路径
      // 前端拿到路径后再通过 WebSocket 调用 skills.import_upload / skills.create_from_knowledge
      server.middlewares.use('/file-api/skills/upload-temp', (req, res) => {
        if (req.method !== 'POST') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }

        const contentType = req.headers['content-type'] || ''
        const chunks: Buffer[] = []

        req.on('data', (chunk: Buffer) => chunks.push(chunk))
        req.on('end', () => {
          try {
            const body = Buffer.concat(chunks)
            const tmpDir = path.join(os.tmpdir(), 'jiuwenswarm_skill_upload')
            if (!fs.existsSync(tmpDir)) {
              fs.mkdirSync(tmpDir, { recursive: true })
            }

            let fileBuffer: Buffer | null = null
            let filename = 'upload.zip'

            if (contentType.includes('multipart/form-data')) {
              const boundaryMatch = contentType.match(/boundary=(?:"([^"]+)"|([^;]+))/)
              if (boundaryMatch) {
                const boundary = boundaryMatch[1] || boundaryMatch[2]
                const boundaryBuf = Buffer.from(`--${boundary}`)
                const parts: Buffer[] = []
                let start = body.indexOf(boundaryBuf) + boundaryBuf.length

                while (start < body.length) {
                  const nextBoundary = body.indexOf(boundaryBuf, start)
                  if (nextBoundary === -1) break
                  parts.push(body.slice(start, nextBoundary))
                  start = nextBoundary + boundaryBuf.length
                }

                for (const part of parts) {
                  const headerEnd = part.indexOf('\r\n\r\n')
                  if (headerEnd === -1) continue
                  const header = part.slice(0, headerEnd).toString('utf-8')
                  const content = part.slice(headerEnd + 4, part.length - 2)
                  const nameMatch = header.match(/name="([^"]+)"/)
                  const filenameMatch = header.match(/filename="([^"]+)"/)
                  if (nameMatch && nameMatch[1] === 'file') {
                    fileBuffer = content
                    if (filenameMatch) filename = filenameMatch[1]
                  }
                }
              }
            } else {
              fileBuffer = body
              const url = new URL(req.url || '/file-api/skills/upload-temp', 'http://localhost')
              const nameParam = url.searchParams.get('filename')
              if (nameParam) filename = nameParam
            }

            if (!fileBuffer) {
              res.statusCode = 400
              res.setHeader('content-type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ error: 'no_file_found' }))
              return
            }

            const safeName = path.basename(filename).replace(/[^a-zA-Z0-9._-]/g, '_')
            const tempPath = path.join(tmpDir, `${Date.now()}_${safeName}`)
            fs.writeFileSync(tempPath, fileBuffer)

            res.statusCode = 200
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ path: tempPath }))
          } catch (error) {
            res.statusCode = 500
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: (error as Error).message }))
          }
        })
      })

      server.middlewares.use('/file-api/file-content', (req, res) => {
        if (req.method === 'GET') {
          const url = new URL(req.url || '/file-api/file-content', 'http://localhost')
          const filePath = url.searchParams.get('path')
          const requestedEncoding = url.searchParams.get('encoding') || 'utf-8'
          if (!filePath) {
            res.statusCode = 400
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: '缺少文件路径' }))
            return
          }

          try {
            const fullPath = path.resolve(projectRootDir, filePath)
            if (!isPathUnderAllowedRoot(fullPath)) {
              res.statusCode = 403
              res.setHeader('content-type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ error: 'forbidden_path' }))
              return
            }
            if (!fs.existsSync(fullPath)) {
              if (filePath.replace(/\\/g, '/') === 'agent/workspace/agent-data.json') {
                try {
                  const runResult = spawnSync(process.execPath, [generateAgentFoldersScriptPath], {
                    encoding: 'utf-8',
                    env: { ...process.env, JIUWENSWARM_ROOT: projectRootDir },
                    cwd: path.dirname(path.dirname(generateAgentFoldersScriptPath)),
                  })
                  if (runResult.status === 0 && fs.existsSync(fullPath)) {
                    const { content, encoding } = decodeFileContent(fs.readFileSync(fullPath), requestedEncoding)
                    res.statusCode = 200
                    res.setHeader('content-type', 'text/plain; charset=utf-8')
                    res.setHeader('X-Original-Encoding', encoding)
                    res.end(content)
                    return
                  }
                } catch {
                  /* fall through to 404 */
                }
              }
              res.statusCode = 404
              res.setHeader('content-type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ error: '文件不存在', fullPath }))
              return
            }

            const { content, encoding } = decodeFileContent(fs.readFileSync(fullPath), requestedEncoding)
            res.statusCode = 200
            res.setHeader('content-type', 'text/plain; charset=utf-8')
            res.setHeader('X-Original-Encoding', encoding)
            res.end(content)
          } catch (error) {
            res.statusCode = 500
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: (error as Error).message }))
          }
          return
        }

        if (req.method !== 'POST') {
          res.statusCode = 405
          res.setHeader('content-type', 'application/json; charset=utf-8')
          res.end(JSON.stringify({ error: 'method_not_allowed' }))
          return
        }

        let raw = ''
        req.on('data', (chunk) => {
          raw += chunk.toString()
        })
        req.on('end', () => {
          let payload: { path?: unknown; content?: unknown } = {}
          try {
            payload = raw ? JSON.parse(raw) : {}
          } catch {
            res.statusCode = 400
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: 'invalid_json' }))
            return
          }

          const requestPath = payload.path
          const requestContent = payload.content
          if (typeof requestPath !== 'string' || !requestPath.trim()) {
            res.statusCode = 400
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: '缺少文件路径' }))
            return
          }
          if (typeof requestContent !== 'string') {
            res.statusCode = 400
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: '缺少文件内容' }))
            return
          }

          const fullPath = path.resolve(projectRootDir, requestPath)
          if (!isPathUnderAllowedRoot(fullPath)) {
            res.statusCode = 403
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: 'forbidden_path' }))
            return
          }
          if (!isMarkdownFile(fullPath)) {
            res.statusCode = 400
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: '仅支持保存 Markdown 文件' }))
            return
          }
          if (!fs.existsSync(fullPath)) {
            res.statusCode = 404
            res.setHeader('content-type', 'application/json; charset=utf-8')
            res.end(JSON.stringify({ error: '文件不存在' }))
            return
          }

          fs.writeFile(fullPath, requestContent, 'utf-8', (error) => {
            if (error) {
              res.statusCode = 500
              res.setHeader('content-type', 'application/json; charset=utf-8')
              res.end(JSON.stringify({ error: error.message }))
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

// https://vitejs.dev/config/
function portFromEnv(name: string, fallback: number): number {
  const value = Number.parseInt(process.env[name] ?? '', 10)
  return Number.isInteger(value) && value > 0 && value <= 65535 ? value : fallback
}

const frontendPort = portFromEnv('FRONTEND_PORT', 5173)
const webPort = portFromEnv('WEB_PORT', 19000)
const webTarget = `http://127.0.0.1:${webPort}`

export default defineConfig({
  plugins: [suppressWsProxySocketErrors(), devWsTrafficLogger(), devFileContentApi(), react(), svgr()],
  optimizeDeps: {
    include: ['exceljs', 'jszip', 'saxes', 'ssf'],
  },
  resolve: {
    dedupe: ['react', 'react-dom'],
    alias: {
      '@': path.resolve(__dirname, './src'),
      'lucide-react': path.resolve(__dirname, './node_modules/lucide-react'),
      react: path.resolve(__dirname, './node_modules/react'),
      'react-dom': path.resolve(__dirname, './node_modules/react-dom'),
    },
  },
  server: {
    host: true,
    allowedHosts: ['127.0.0.1'],
    port: frontendPort,
    strictPort: true,
    proxy: {
      '/api': {
        target: webTarget,
        changeOrigin: true,
      },
      '/skillhub-api': {
        target: 'http://119.8.233.112:8080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/skillhub-api/, '/api/v1'),
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
