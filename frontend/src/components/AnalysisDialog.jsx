import { useEffect, useRef, useState } from 'react'
import { motion } from 'framer-motion'
import { AlertTriangle, Check, Crosshair, Flame, Loader2, MapPin, Search, ShieldCheck } from 'lucide-react'
import { api, apiUpload } from '../api'
import { Badge, Btn, Card, spring } from '../ui'
import MiniMap from './MiniMap'

const pct = (c) => `${Math.round((c || 0) * 100)}%`

/**
 * Guided intake: upload → model → show the annotated frame → confirm the
 * shooting location → create the incident.
 *
 * Stage 1 ("upload")  real byte progress from XHR, then an indeterminate bar
 *                     while the model runs.
 * Stage 2 ("review")  the annotated image with fire/smoke boxes, and a mandatory
 *                     location step: EXIF may be absent or wrong.
 */
export default function AnalysisDialog({ t, file, onCancel, onCreated }) {
  const [stage, setStage] = useState('upload')
  const [progress, setProgress] = useState(0)
  const [draft, setDraft] = useState(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const [lat, setLat] = useState('')
  const [lon, setLon] = useState('')
  const [editing, setEditing] = useState(false)
  const [q, setQ] = useState('')
  const [places, setPlaces] = useState([])
  const alive = useRef(true)

  useEffect(() => () => { alive.current = false }, [])

  // 1) upload + analyse
  useEffect(() => {
    if (!file || !alive.current) return
    const ctrl = new AbortController()
    const fd = new FormData()
    fd.append('file', file)
    apiUpload('/incidents/analyze', fd, {
      signal: ctrl.signal,
      onProgress: (v) => { if (alive.current) setProgress(v) },
    })
      .then((d) => {
        if (!alive.current) return
        setDraft(d)
        setLat(d.lat ?? '')
        setLon(d.lon ?? '')
        // No EXIF at all → the operator must set the point; start in edit mode.
        setEditing(!d.has_exif)
        setStage('review')
      })
      .catch((e) => { if (alive.current) { setErr(e.message); setStage('error') } })
    return () => ctrl.abort()
  }, [file])

  // 2) city search inside the review step
  useEffect(() => {
    if (q.trim().length < 2) { setPlaces([]); return }
    const ctrl = new AbortController()
    const timer = setTimeout(async () => {
      try {
        const rows = await api(`/geo/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal, timeoutMs: 15000 })
        setPlaces(Array.isArray(rows) ? rows : [])
      } catch (e) { if (e.status !== 0) setPlaces([]) }
    }, 250)
    return () => { clearTimeout(timer); ctrl.abort() }
  }, [q])

  const pick = (p) => { setLat(p.lat); setLon(p.lon); setQ(p.name.split(', ')[0]); setPlaces([]) }
  const geo = () => navigator.geolocation.getCurrentPosition(
    (p) => { setLat(p.coords.latitude); setLon(p.coords.longitude) },
    () => setErr(t.geoFail))

  const confirm = async () => {
    const la = parseFloat(lat), lo = parseFloat(lon)
    const hasPoint = Number.isFinite(la) && Number.isFinite(lo)
    if (!hasPoint) { setErr(t.location_required); return }
    setBusy(true); setErr('')
    try {
      const row = await api('/incidents/confirm', {
        json: { draft_id: draft.draft_id, lat: la, lon: lo },
        timeoutMs: 300000,
      })
      onCreated?.(row)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  const dets = draft?.detections || []
  const point = (() => {
    const la = parseFloat(lat), lo = parseFloat(lon)
    return Number.isFinite(la) && Number.isFinite(lo) ? [la, lo] : null
  })()
  const coordText = point ? `${point[0].toFixed(6)}, ${point[1].toFixed(6)}` : '—' 

  return (
    <div className="space-y-3" role="dialog" aria-label={t.analyze_result}>
      {stage === 'upload' && (
        <div className="space-y-3 py-4">
          <p className="flex items-center gap-2 font-semibold">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            {progress < 100 ? t.uploading : t.analyzing}
          </p>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <motion.div className="h-full rounded-full bg-primary" animate={{ width: progress < 100 ? `${progress}%` : '100%' }}
              transition={spring} />
          </div>
          <div className="flex justify-between font-mono text-xs text-muted-foreground">
            <span>{progress < 100 ? `${progress}%` : t.analyzing}</span>
            <span>{t.processing_hint}</span>
          </div>
          <Btn v="ghost" onClick={onCancel}>{t.cancel}</Btn>
        </div>
      )}

      {stage === 'error' && (
        <div className="space-y-3 py-2">
          <p role="alert" className="text-sm text-destructive">{err}</p>
          <Btn onClick={onCancel}>{t.close}</Btn>
        </div>
      )}

      {stage === 'review' && draft && (
        <>
          <div className="overflow-hidden rounded-xl border border-border/50">
            <img src={draft.annotated_url} alt="" className="w-full object-contain" />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <Badge status={draft.status}>{t['s_' + draft.status]}</Badge>
            <span className="font-mono text-xs">{t.conf}: {pct(draft.confidence)}</span>
            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
              <Flame className="h-3.5 w-3.5" aria-hidden />
              {dets.length ? `${t.detected}: ${dets.length}` : t.nothing_detected}
            </span>
          </div>

          {dets.length > 0 && (
            <ul className="max-h-24 space-y-0.5 overflow-y-auto font-mono text-xs text-muted-foreground">
              {dets.slice(0, 8).map((d, k) => <li key={k}>{d.cls} — {pct(d.conf)}</li>)}
            </ul>
          )}

          <Card lift={false} className="space-y-2 p-3">
            {draft.has_exif && !editing ? (
              <>
                <p className="flex items-center gap-2 text-sm font-semibold">
                  <ShieldCheck className="h-4 w-4 text-primary" aria-hidden />{t.exif_found}
                </p>
                <p className="font-mono text-sm">{Number(draft.lat).toFixed(6)}, {Number(draft.lon).toFixed(6)}</p>
                <p className="text-sm">{t.confirm_question}</p>
                <div className="flex flex-wrap gap-2">
                  <Btn v="ghost" onClick={() => setEditing(true)}><Crosshair className="h-4 w-4" aria-hidden />{t.confirm_edit}</Btn>
                </div>
              </>
            ) : (
              <>
                <p className={`flex items-center gap-2 text-sm font-semibold ${draft.has_exif ? '' : 'text-destructive'}`}>
                  {draft.has_exif ? <MapPin className="h-4 w-4" aria-hidden /> : <AlertTriangle className="h-4 w-4" aria-hidden />}
                  {draft.has_exif ? t.confirm_edit : t.no_exif_title}
                </p>
                {!draft.has_exif && <p className="text-xs text-muted-foreground">{t.no_exif_body}</p>}

                {/* Primary way to set the point: click the map. */}
                <MiniMap t={t} value={point} onChange={(p) => { setLat(p[0]); setLon(p[1]); setPlaces([]) }} />

                <label className="flex items-center gap-2 rounded-full border border-border/60 px-3">
                  <Search className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                  <input className="min-h-11 w-full border-0 bg-transparent text-sm outline-none"
                    placeholder={t.search_ph} value={q} onChange={(e) => setQ(e.target.value)} />
                </label>
                {places.length > 0 && (
                  <ul className="max-h-40 overflow-auto rounded-xl border border-border/50 text-sm">
                    {places.map((p, k) => (
                      <li key={k}><button type="button" onClick={() => pick(p)}
                        className="min-h-11 w-full cursor-pointer px-3 py-2 text-left hover:bg-muted">{p.name}</button></li>
                    ))}
                  </ul>
                )}
                <div className="flex flex-wrap items-end gap-2">
                  <label className="block space-y-1"><span className="text-xs font-semibold text-muted-foreground">{t.coords_from_map}</span>
                    <input className="field w-64 font-mono" value={coordText} readOnly tabIndex={-1} /></label>
                  <Btn v="ghost" onClick={geo}><Crosshair className="h-4 w-4" aria-hidden />{t.geo}</Btn>
                  {draft.has_exif && (
                    <Btn v="ghost" onClick={() => {
                      setEditing(false); setLat(draft.lat); setLon(draft.lon)
                    }}>{t.cancel}</Btn>
                  )}
                </div>
              </>
            )}
          </Card>

          {err && <p role="alert" className="text-sm text-destructive">{err}</p>}

          <div className="flex flex-wrap gap-2">
            <Btn disabled={busy} onClick={confirm}>
              <Check className="h-4 w-4" aria-hidden />{busy ? t.wait : t.create_incident}
            </Btn>
            <Btn v="ghost" onClick={onCancel}>{t.cancel}</Btn>
          </div>
        </>
      )}
    </div>
  )
}
