import type { Plugin } from 'vite'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { spawnSync } from 'child_process'
import path from 'path'
import fs from 'fs'

type ConfigWithLogger = { logger?: { error?: (msg: string, opts?: { error?: Error }) => void } }

interface ErrorWithCode {
  code?: string
}

/**
 * file-api 使用的项目根目录，需与后端 get_root_dir() 一致，前端编辑的 HEARTBEAT.md 才会被心跳读到。
 * 优先级：环境变量 > 已存在的用户工作区 ~/.jiuwenclaw > 仓库根。
 */
function resolveProjectRootDir(): string {
  const envRoot = process.env.JIUWENCLAW_ROOT || process.env.JIUWENCLAW_PROJECT_ROOT
  if (envRoot) {
    const resolved = path.resolve(envRoot)
    console.log('[file-api] 使用环境变量根目录:', resolved)
    return resolved
  }
  const home = process.env.USERPROFILE || process.env.HOME || ''
  if (home) {
    const userWorkspace = path.join(home, '.jiuwenclaw')
    if (fs.existsSync(userWorkspace)) {
      console.log('[file-api] 使用用户工作区:', path.resolve(userWorkspace))
      return path.resolve(userWorkspace)
    }
  }
  const repoRoot = path.resolve(__dirname, '../..')
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
      const logDir = path.resolve(projectRootDir, '.logs')
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
          const line = `${JSON.stringify({ ts: now, payload })}\n`
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
  const webLogsRootDir = path.resolve(projectRootDir, '.logs')
  const generateAgentFoldersScriptPath = path.resolve(__dirname, '../scripts/generate-agent-folders.js')
  // dev 模式默认开启调试视图，与“前端 dev 即调试模式”一致。
  let wsDisableCompress = true
  const isMarkdownFile = (targetPath: string) => {
    const ext = path.extname(targetPath).toLowerCase()
    return ext === '.md' || ext === '.mdx'
  }
  const isPathUnderAllowedRoot = (targetPath: string) => {
    const relativeWorkspacePath = path.relative(workspaceRootDir, targetPath)
    const inWorkspace = !relativeWorkspacePath.startsWith('..') && !path.isAbsolute(relativeWorkspacePath)
    const relativeLogsPath = path.relative(webLogsRootDir, targetPath)
    const inWebLogs = !relativeLogsPath.startsWith('..') && !path.isAbsolute(relativeLogsPath)
    return inWorkspace || inWebLogs
  }

  return {
    name: 'dev-file-content-api',
    configureServer(server) {
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

      server.middlewares.use('/file-api/file-content', (req, res) => {
        if (req.method === 'GET') {
          const url = new URL(req.url || '/file-api/file-content', 'http://localhost')
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
            if (!fs.existsSync(fullPath)) {
              if (filePath.replace(/\\/g, '/') === 'agent/jiuwenclaw_workspace/agent-data.json') {
                try {
                  const runResult = spawnSync(process.execPath, [generateAgentFoldersScriptPath], {
                    encoding: 'utf-8',
                    env: { ...process.env, JIUWENCLAW_ROOT: projectRootDir },
                    cwd: path.dirname(path.dirname(generateAgentFoldersScriptPath)),
                  })
                  if (runResult.status === 0 && fs.existsSync(fullPath)) {
                    const content = fs.readFileSync(fullPath, 'utf-8')
                    res.statusCode = 200
                    res.setHeader('content-type', 'text/plain; charset=utf-8')
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

            const content = fs.readFileSync(fullPath, 'utf-8')
            res.statusCode = 200
            res.setHeader('content-type', 'text/plain; charset=utf-8')
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
export default defineConfig({
  plugins: [suppressWsProxySocketErrors(), devWsTrafficLogger(), devFileContentApi(), react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,  // 默认端口
    strictPort: true,  // 强制使用 5173 端口
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:19000',
        changeOrigin: true,
      },
      '/ws': {
        target: 'http://127.0.0.1:19000',
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
