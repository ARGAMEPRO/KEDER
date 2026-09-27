const BASE = import.meta.env.VITE_API_URL || '/api/v1'
export const tok = {
  get: () => localStorage.getItem('kedr_token'),
  set: (v) => (v ? localStorage.setItem('kedr_token', v) : localStorage.removeItem('kedr_token')),
}
export async function api(path, { json, body, raw, method } = {}) {
  const headers = {}
  if (tok.get()) headers.Authorization = `Bearer ${tok.get()}`
  if (json) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(json) }
  const m = method || (body != null ? 'POST' : 'GET')
  const r = await fetch(BASE + path, { method: m, headers, body: body ?? undefined })
  if (!r.ok) {
    const d = (await r.json().catch(() => ({}))).detail
    throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : r.statusText)
  }
  return raw ? r.blob() : r.json()
}
export const wsUrl = () =>
  `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/alerts?token=${tok.get()}`
