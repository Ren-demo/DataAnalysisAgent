/**
 * 生产环境静态服务 + /api 反向代理（替代 nginx）。
 *
 * 浏览器只访问本进程，因此：
 *   1) 前端构建产物 dist/ 由本进程直接伺服；
 *   2) /api/** 原样转发给后端 FastAPI，前端代码无需改 API 地址，也不产生 CORS；
 *   3) SSE（/api/chat/stream）字节级透传、不缓冲，后端 `: keepalive` 心跳可即时到达浏览器。
 *
 * 零第三方依赖：只用 Node 内置模块，运行时镜像无需 npm install。
 */
import { createServer, request as httpRequest } from 'node:http'
import { createReadStream } from 'node:fs'
import { readFile, stat } from 'node:fs/promises'
import { promisify } from 'node:util'
import { gzip } from 'node:zlib'
import { extname, join, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const gzipAsync = promisify(gzip)

const PORT = Number(process.env.PORT || 3000)
const HOST = process.env.HOST || '0.0.0.0'
const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8000'
// 必须解析成绝对路径：resolveStaticPath 用 resolve(STATIC_DIR, ...) 得到的也是绝对路径，
// 若这里保留相对值（如本地调试的 STATIC_DIR=dist），前缀校验会永远不匹配 → 全部 400。
const STATIC_DIR = resolve(
  process.env.STATIC_DIR ||
    join(fileURLToPath(new URL('.', import.meta.url)), 'dist'),
)

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.map': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.ttf': 'font/ttf',
  '.txt': 'text/plain; charset=utf-8',
  '.webmanifest': 'application/manifest+json',
}

/** 可压缩类型（SVG 是文本，值得压缩；图片/字体已是压缩格式，不再压）。 */
const COMPRESSIBLE = /^(text\/|application\/(javascript|json|xml|manifest\+json)|image\/svg\+xml)/

/** gzip 结果缓存：dist 是只读的，进程存活期间内容不会变。 */
const gzipCache = new Map()

/**
 * 把 URL 路径解析为 dist 内的真实文件路径。
 * 拒绝目录穿越（`..`、编码后的 `%2e%2e`、NUL 字节）。
 */
function resolveStaticPath(pathname) {
  let decoded
  try {
    decoded = decodeURIComponent(pathname)
  } catch {
    return null
  }
  if (decoded.includes('\0')) return null

  // 统一成「以 / 开头」的相对路径后交给 resolve；
  // 再显式校验结果仍在 STATIC_DIR 内（resolve 会吃掉 `..`）。
  const relative = decoded.startsWith('/') ? decoded : `/${decoded}`
  const full = resolve(STATIC_DIR, `.${relative}`)
  return full === STATIC_DIR || full.startsWith(STATIC_DIR + sep) ? full : null
}

async function gzipFile(filePath, info) {
  const cached = gzipCache.get(filePath)
  if (cached && cached.mtimeMs === info.mtimeMs && cached.size === info.size) {
    return cached.body
  }
  const body = await gzipAsync(await readFile(filePath))
  // 只缓存中小文件，避免把超大产物长期留在内存里
  if (info.size <= 8 * 1024 * 1024) {
    gzipCache.set(filePath, { mtimeMs: info.mtimeMs, size: info.size, body })
  }
  return body
}

/**
 * 发送一个静态文件。返回 false 表示「文件不存在或不是文件」，由调用方决定 404 / SPA 回退。
 */
async function sendFile(req, res, filePath, status = 200) {
  const info = await stat(filePath).catch(() => null)
  if (!info || !info.isFile()) return false

  const ext = extname(filePath).toLowerCase()
  const type = MIME[ext] || 'application/octet-stream'
  const etag = `W/"${info.size}-${Math.floor(info.mtimeMs)}"`

  if (req.headers['if-none-match'] === etag) {
    res.writeHead(304, { ETag: etag })
    res.end()
    return true
  }

  // Vite 的 assets/ 下文件名带内容哈希，可长期强缓存；index.html 必须每次回源。
  const hashed = filePath.includes(`${sep}assets${sep}`)
  const headers = {
    'Content-Type': type,
    'Cache-Control': hashed
      ? 'public, max-age=31536000, immutable'
      : 'no-cache',
    ETag: etag,
    Vary: 'Accept-Encoding',
  }

  const acceptsGzip = /\bgzip\b/.test(req.headers['accept-encoding'] || '')
  if (acceptsGzip && COMPRESSIBLE.test(type) && info.size > 1024) {
    const body = await gzipFile(filePath, info)
    headers['Content-Encoding'] = 'gzip'
    headers['Content-Length'] = body.length
    res.writeHead(status, headers)
    res.end(req.method === 'HEAD' ? undefined : body)
    return true
  }

  headers['Content-Length'] = info.size
  res.writeHead(status, headers)
  if (req.method === 'HEAD') {
    res.end()
    return true
  }
  createReadStream(filePath)
    .on('error', () => res.destroy())
    .pipe(res)
  return true
}

async function serveStatic(req, res, pathname) {
  let filePath = resolveStaticPath(pathname)
  if (!filePath) {
    res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end('Bad Request')
    return
  }

  // /foo/ 与 /foo 都尝试目录下的 index.html
  const info = await stat(filePath).catch(() => null)
  if (info && info.isDirectory()) {
    filePath = join(filePath, 'index.html')
  } else if (!info && !extname(filePath)) {
    // 无扩展名的路径按 SPA 导航处理
    filePath = join(STATIC_DIR, 'index.html')
  }

  if (await sendFile(req, res, filePath)) return

  // 仅对「浏览器导航」回退首页；缺失的 .js/.css 必须返回 404，
  // 否则浏览器会把 HTML 当脚本执行，报出难以定位的 MIME 错误。
  const accept = req.headers.accept || ''
  if (req.method === 'GET' && accept.includes('text/html')) {
    if (await sendFile(req, res, join(STATIC_DIR, 'index.html'))) return
  }

  res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
  res.end('Not Found')
}

/**
 * 反向代理到 FastAPI。
 *
 * SSE 要点：
 * - 不读完整响应体、不做压缩、不设 timeout，上游写多少就往下游转多少；
 * - 立即 flush 响应头，让浏览器尽早进入流式读取；
 * - 客户端断开（用户点「停止」/ 关页面）时销毁上游请求，
 *   避免后端 agent 继续空转烧 token。
 */
function proxyToBackend(req, res, rawUrl) {
  const target = new URL(rawUrl, BACKEND_URL)

  const clientIp =
    req.socket.remoteAddress?.replace(/^::ffff:/, '') || 'unknown'
  const headers = {
    ...req.headers,
    host: target.host,
    'x-forwarded-for': req.headers['x-forwarded-for']
      ? `${req.headers['x-forwarded-for']}, ${clientIp}`
      : clientIp,
    'x-forwarded-proto': req.headers['x-forwarded-proto'] || 'http',
    'x-forwarded-host': req.headers.host || '',
  }
  // 逐跳头不能转发
  delete headers.connection
  delete headers['keep-alive']
  delete headers['proxy-connection']
  delete headers['transfer-encoding']
  delete headers.upgrade

  const upstream = httpRequest(
    {
      protocol: target.protocol,
      hostname: target.hostname,
      port: target.port || (target.protocol === 'https:' ? 443 : 80),
      method: req.method,
      path: target.pathname + target.search,
      headers,
      timeout: 0, // 长任务（分析 / 报告 agent）不设上游超时
    },
    (upRes) => {
      const responseHeaders = { ...upRes.headers }
      delete responseHeaders.connection
      delete responseHeaders['keep-alive']
      delete responseHeaders['transfer-encoding']

      // 注意：writeHead(status, statusMessage, headers) 的第二个参数不是字符串时，
      // Node 会把它当成 headers、忽略第三个参数 —— statusMessage 常常是 undefined，
      // 直接传三参会把真正的响应头（含 SSE 的 content-type）整份丢掉。
      const statusCode = upRes.statusCode || 502
      if (upRes.statusMessage) {
        res.writeHead(statusCode, upRes.statusMessage, responseHeaders)
      } else {
        res.writeHead(statusCode, responseHeaders)
      }
      res.socket?.setNoDelay(true)
      res.flushHeaders()
      upRes.pipe(res)
    },
  )

  upstream.on('error', (err) => {
    // 客户端已断开（用户点「停止」/关页面）时 res 已销毁，再写 502 只会产生二次异常
    if (res.destroyed || res.writableEnded) return
    if (res.headersSent) {
      res.destroy()
      return
    }
    console.error('[proxy] 后端请求失败:', err.message)
    res.writeHead(502, {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
    })
    res.end(
      JSON.stringify({
        error: '无法连接后端服务，请确认 backend 容器已启动。',
      }),
    )
  })

  // 客户端中途断开：销毁上游，避免孤儿请求继续消耗 LLM 额度
  res.on('close', () => {
    if (!res.writableEnded) upstream.destroy()
  })
  req.on('aborted', () => upstream.destroy())

  req.pipe(upstream)
}

const server = createServer(async (req, res) => {
  try {
    const rawUrl = req.url || '/'
    const queryIndex = rawUrl.indexOf('?')
    const rawPath = queryIndex >= 0 ? rawUrl.slice(0, queryIndex) : rawUrl

    // 这里解码一次仅用于「路由判断」；静态文件路径交给 serveStatic 解码，
    // 避免重复解码把含 `%` 的合法文件名（如 `100%.js`）误判成非法 URL。
    let pathname
    try {
      pathname = decodeURIComponent(rawPath)
    } catch {
      res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' })
      res.end('Bad Request')
      return
    }

    // 前端容器自身的存活探针，不依赖后端
    if (pathname === '/healthz') {
      res.writeHead(200, {
        'Content-Type': 'text/plain; charset=utf-8',
        'Cache-Control': 'no-store',
      })
      res.end('ok')
      return
    }

    if (pathname === '/api' || pathname.startsWith('/api/')) {
      proxyToBackend(req, res, rawUrl)
      return
    }

    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.writeHead(405, { Allow: 'GET, HEAD' })
      res.end('Method Not Allowed')
      return
    }

    // 传未解码的原始路径：serveStatic → resolveStaticPath 内部只解码一次
    await serveStatic(req, res, rawPath)
  } catch (err) {
    console.error('[server] 未处理的异常:', err)
    if (!res.headersSent) {
      res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' })
    }
    res.end('Internal Server Error')
  }
})

// requestTimeout 只覆盖「接收请求（头 + 体）」的时长，不覆盖响应耗时，
// 因此放开到 10 分钟只为照顾慢速网络下的 20MB 上传；SSE 长任务不受它约束。
server.requestTimeout = 10 * 60 * 1000
server.headersTimeout = 60 * 1000

// server.timeout 默认 0（不超时），正是长连接 SSE 需要的。
server.listen(PORT, HOST, () => {
  console.log(`[server] 静态资源目录: ${STATIC_DIR}`)
  console.log(`[server] /api 反向代理 -> ${BACKEND_URL}`)
  console.log(`[server] 监听 http://${HOST}:${PORT}`)
})