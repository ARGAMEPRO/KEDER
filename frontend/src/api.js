const BASE = import.meta.env.VITE_API_URL || '/api/v1'
const TOKEN_KEY = 'kedr_token'

/** Dispatched when the server rejects our token, so the app can log out cleanly. */
export const UNAUTHORIZED_EVENT = 'kedr:unauthorized'

export const tok = {
  get: () => {
    try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
  },
  set: (v) => {
    try { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY) } catch { /* private mode */ }
  },
}

function errorMessage(status, detail, statusText) {
  if (typeof detail === 'string' && detail) return detail
  if (Array.isArray(detail) && detail.length) {
    // FastAPI validation errors: [{loc, msg, type}]
    return detail.map((d) => d.msg || JSON.stringify(d)).join('; ')
  }
  if (detail) return JSON.stringify(detail)
  if (status === 429) return 'Слишком много запросов, попробуйте через минуту'
  if (status === 503) return 'Сервис временно недоступен'
  return statusText || `Ошибка запроса (${status})`
}

/**
 * Fetch wrapper: bearer auth, JSON handling, real timeouts and clean 401 handling.
 * Every call is abortable and never leaves a hanging request behind.
 */
export async function api(path, { json, body, raw, method, timeoutMs = 30000, signal } = {}) {
  const controller = new AbortController()
  const timer = timeoutMs ? setTimeout(() => controller.abort(), timeoutMs) : null
  const forwardAbort = () => controller.abort()
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', forwardAbort, { once: true })
  }

  const headers = {}
  const token = tok.get()
  if (token) headers.Authorization = `Bearer ${token}`
  if (json) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(json)
  }
  const verb = method || (body != null ? 'POST' : 'GET')

  try {
    const r = await fetch(BASE + path, { method: verb, headers, body: body ?? undefined, signal: controller.signal })
    if (r.status === 401) {
      tok.set(null)
      try { dispatchEvent(new Event(UNAUTHORIZED_EVENT)) } catch { /* non-browser env */ }
    }
    if (!r.ok) {
      const detail = await r.json().then((d) => d?.detail).catch(() => null)
      const err = new Error(errorMessage(r.status, detail, r.statusText))
      err.status = r.status
      throw err
    }
    if (raw) return r.blob()
    if (r.status === 204) return null
    const text = await r.text()
    return text ? JSON.parse(text) : null
  } catch (e) {
    if (e.name === 'AbortError') {
      const err = new Error('Превышено время ожидания ответа сервера')
      err.status = 0
      throw err
    }
    throw e
  } finally {
    if (timer) clearTimeout(timer)
    if (signal) signal.removeEventListener('abort', forwardAbort)
  }
}

/**
 * Staff alert stream.
 *
 * The JWT is sent in the first frame over an accepted "kedr.v1" subprotocol, so it
 * never appears in a URL or in proxy access logs. Reconnects use exponential backoff
 * with jitter, and a keep-alive keeps idle proxies from dropping the socket.
 */
export function connectAlerts({ onMessage, onStatus, token }) {
  let ws = null
  let closed = false
  let attempt = 0
  let keepAlive = null
  let retryTimer = null

  const clearTimers = () => {
    if (keepAlive) { clearInterval(keepAlive); keepAlive = null }
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null }
  }

  // Function declarations (not const arrows) so the mutual recursion below is
  // hoisted and cannot hit a temporal dead zone.
  function scheduleRetry() {
    if (closed) return
    attempt += 1
    const base = Math.min(30000, 1000 * 2 ** Math.min(attempt, 5))
    const delay = base + Math.random() * 1000
    retryTimer = setTimeout(open, delay)
  }

  function open() {
    if (closed) return
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    try {
      ws = new WebSocket(`${proto}://${location.host}/ws/alerts`, 'kedr.v1')
    } catch {
      scheduleRetry()
      return
    }
    ws.onopen = () => {
      attempt = 0
      onStatus?.('open')
      try { ws.send(JSON.stringify({ type: 'auth', token })) } catch { /* ignore */ }
      keepAlive = setInterval(() => {
        try { ws.send(JSON.stringify({ type: 'ping' })) } catch { /* ignore */ }
      }, 30000)
    }
    ws.onmessage = (e) => {
      let msg
      try { msg = JSON.parse(e.data) } catch { return }  // never let a bad frame throw
      if (msg && msg.type && msg.type !== 'pong') onMessage?.(msg)
    }
    ws.onerror = () => { onStatus?.('error') }
    ws.onclose = (ev) => {
      clearTimers()
      onStatus?.('closed')
      if (closed) return
      // 4401 = token rejected: retrying will not help until the user signs in again.
      if (ev?.code === 4401) return
      scheduleRetry()
    }
  }

  open()
  return () => {
    closed = true
    clearTimers()
    try { ws?.close() } catch { /* ignore */ }
  }
}


/**
 * Upload with real progress events. `fetch` cannot report upload progress, so
 * this uses XMLHttpRequest; the response contract matches api().
 */
export function apiUpload(path, formData, { onProgress, signal, timeoutMs = 300000 } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', BASE + path, true)
    const token = tok.get()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.timeout = timeoutMs
    if (onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100))
      }
    }
    xhr.onload = () => {
      let body = null
      try { body = xhr.responseText ? JSON.parse(xhr.responseText) : null } catch { /* non-JSON */ }
      if (xhr.status === 401) { tok.set(null); try { dispatchEvent(new Event(UNAUTHORIZED_EVENT)) } catch { /* ignore */ } }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(body); return }
      const detail = body?.detail
      const err = new Error(typeof detail === 'string' && detail ? detail : `Ошибка запроса (${xhr.status})`)
      err.status = xhr.status
      reject(err)
    }
    xhr.onerror = () => reject(new Error('Сеть недоступна'))
    xhr.ontimeout = () => reject(new Error('Превышено время ожидания'))
    xhr.onabort = () => reject(new Error('Загрузка отменена'))
    if (signal) signal.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(formData)
  })
}
