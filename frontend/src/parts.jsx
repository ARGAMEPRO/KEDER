import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { ArrowUp, Copy, Crosshair, FileDown, MapPin, Navigation, RefreshCw, Trash2 } from 'lucide-react'
import { api } from './api'
import { Badge, Btn, Card } from './ui'

export const pct = (c) => `${Math.round(c * 100)}%`

export function Coord({ i, t }) {
  const s = `${i.lat.toFixed(6)}, ${i.lon.toFixed(6)}`
  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="font-mono text-sm">{s}</span>
      <Btn v="ghost" onClick={() => navigator.clipboard.writeText(s)}><Copy className="h-4 w-4" aria-hidden />{t.copy}</Btn>
      <Btn v="ghost" href={`https://yandex.ru/maps/?pt=${i.lon},${i.lat}&z=14&l=map`} target="_blank" rel="noreferrer"><Navigation className="h-4 w-4" aria-hidden />{t.nav}</Btn>
    </div>
  )
}

export function FixLocation({ i, t, onFixed }) {
  const [open, setOpen] = useState(i.lat == null), [q, setQ] = useState(''), [places, setPlaces] = useState([])
  const [lat, setLat] = useState(i.lat ?? ''), [lon, setLon] = useState(i.lon ?? '')
  const [busy, setBusy] = useState(false), [err, setErr] = useState('')
  useEffect(() => {
    if (q.trim().length < 2) { setPlaces([]); return }
    const ctrl = new AbortController()
    const t = setTimeout(async () => {
      try {
        const r = await fetch(`/api/v1/geo/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal })
        if (r.ok) setPlaces(await r.json())
      } catch (e) {
        if (e.name !== 'AbortError') setPlaces([])
      }
    }, 250)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [q])
  const geo = () => navigator.geolocation.getCurrentPosition(
    (p) => { setLat(p.coords.latitude); setLon(p.coords.longitude) }, () => setErr(t.geoFail))
  const pickPlace = (p) => { setLat(p.lat); setLon(p.lon); setQ(p.name.split(', ')[0]); setPlaces([]) }
  const save = async () => {
    const la = parseFloat(lat), lo = parseFloat(lon)
    if (!Number.isFinite(la) || !Number.isFinite(lo)) { setErr(t.loc_required); return }
    setBusy(true); setErr('')
    try { const row = await api(`/incidents/${i.id}/location`, { method: 'PATCH', json: { lat: la, lon: lo } }); onFixed?.(row); setOpen(false) }
    catch (e) { setErr(e.message) }
    setBusy(false)
  }
  if (!open) return (
    <Btn v="ghost" onClick={() => setOpen(true)}><MapPin className="h-4 w-4" aria-hidden />{t.fix_location}</Btn>
  )
  return (
    <div className="space-y-2 rounded-xl border border-border/60 bg-muted p-3">
      <p className="text-sm font-semibold">{t.step_place}</p>
      <label className="block space-y-1"><span className="text-xs font-semibold text-muted-foreground">{t.loc_search}</span>
        <input className="field" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t.search_ph} /></label>
      {places.length > 0 && (
        <ul className="max-h-40 overflow-auto rounded-xl border border-border/50 bg-card text-sm">
          {places.map((p, k) => <li key={k}><button type="button" className="min-h-11 w-full cursor-pointer px-3 py-2 text-left hover:bg-muted" onClick={() => pickPlace(p)}>{p.name}</button></li>)}
        </ul>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <label className="block space-y-1"><span className="text-xs font-semibold text-muted-foreground">{t.lat_label}</span>
          <input className="field w-32 font-mono" value={lat} inputMode="decimal" onChange={(e) => setLat(e.target.value)} /></label>
        <label className="block space-y-1"><span className="text-xs font-semibold text-muted-foreground">{t.lon_label}</span>
          <input className="field w-32 font-mono" value={lon} inputMode="decimal" onChange={(e) => setLon(e.target.value)} /></label>
        <Btn v="ghost" onClick={geo}><Crosshair className="h-4 w-4" aria-hidden />{t.geo}</Btn>
      </div>
      <div className="flex flex-wrap gap-2">
        <Btn disabled={busy} onClick={save}>{busy ? t.wait : t.save_location}</Btn>
        <Btn v="ghost" onClick={() => setOpen(false)}>{t.close}</Btn>
      </div>
      {err && <p role="alert" className="text-sm text-destructive">{err}</p>}
    </div>
  )
}

export function Incident({ i: init, t, canPdf, onFocus, onDelete }) {
  const [i, setI] = useState(init), [busy, setBusy] = useState(false)
  useEffect(() => setI(init), [init])
  const refresh = async () => { setBusy(true); try { setI(await api(`/incidents/${i.id}/refresh`, { method: 'POST', json: {} })) } catch { /* keep card */ } setBusy(false) }
  const pdf = async () => { const b = await api(`/incidents/${i.id}/report.pdf`, { raw: true }); const a = document.createElement('a'); a.href = URL.createObjectURL(b); a.download = `kedr-${i.id}.pdf`; a.click() }
  const h = i.hazard_geojson?.properties || {}
  return (
    <Card lift={false} className="space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2"><span className="font-mono text-sm text-muted-foreground">#{i.id}</span><Badge status={i.status}>{t['s_' + i.status]}</Badge></div>
        <span className="flex flex-wrap items-center gap-2 font-mono text-sm">{i.source === 'MANUAL' ? t.source_manual : `${t.source_ai} ${pct(i.confidence)}`}{i.gps_source && <Badge>{(t['gps_' + i.gps_source] || i.gps_source)}</Badge>}</span>
      </div>
      {i.lat != null ? <Coord i={i} t={t} /> : <FixLocation i={i} t={t} onFixed={setI} />}
      {i.region_name && <p className="text-sm text-muted-foreground">{i.region_name}</p>}
      {i.lat != null && (i.wind_speed_ms != null && i.wind_dir_deg != null ? (
        <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-sm">
          <span>{t.wind}: {i.wind_speed_ms} m/s, {i.wind_dir_deg}°</span><span>{t.temp}: {i.temp_c}°C</span><span>{t.hum}: {i.humidity_pct}%</span>
        </div>
      ) : (
        <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-border/60 bg-muted p-3 text-sm">
          {t.wx_off}<Btn v="ghost" disabled={busy} onClick={refresh}><RefreshCw className="h-4 w-4" aria-hidden />{t.retry}</Btn>
        </div>
      ))}
      {h.mode === 'circle' && <p className="text-sm text-muted-foreground">{t.circle_note}</p>}
      {i.lat != null && (
        <div className="space-y-2">
          <h3 className="font-semibold">{t.evac}</h3>
          {i.evacuation?.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead><tr className="text-left text-muted-foreground"><th className="pr-3">{t.evac}</th><th className="pr-3">{t.pop}</th><th className="pr-3">{t.dist}</th><th>{t.evac_dir}</th></tr></thead>
                <tbody>{i.evacuation.map((r) => (
                  <tr key={r.name + r.distance_km} className="border-t border-border/30"><td className="py-1 pr-3">{r.name}</td><td className="pr-3 font-mono">{r.population ?? t.na}</td><td className="pr-3 font-mono">{r.distance_km}</td>
                    <td className="font-mono"><motion.span className="mr-1 inline-block" initial={{ rotate: 0 }} animate={{ rotate: r.evac_bearing ?? r.bearing }} transition={{ type: 'spring', stiffness: 120 }}><ArrowUp className="h-3 w-3" aria-hidden /></motion.span>{r.evac_bearing ?? r.bearing}°</td></tr>))}</tbody>
              </table>
            </div>
          ) : <p className="text-sm text-muted-foreground">{t.no_places}</p>}
          {i.hazard_geojson?.properties?.settlements && (
            <p className="text-xs text-muted-foreground">
              {i.hazard_geojson.properties.settlements === 'osm' ? t.src_osm : t.src_local}
            </p>
          )}
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        {onFocus && i.lat != null && <Btn v="ghost" onClick={() => onFocus(i.id)}><Crosshair className="h-4 w-4" aria-hidden />{t.open_map}</Btn>}
        {canPdf && i.lat != null && <Btn onClick={pdf}><FileDown className="h-4 w-4" aria-hidden />{t.pdf}</Btn>}
        {onDelete && <Btn v="warn" onClick={onDelete}><Trash2 className="h-4 w-4" aria-hidden />{t.delete}</Btn>}
      </div>
    </Card>
  )
}

export function Report({ t, multiple, onDone, canPdf }) {
  const [files, setFiles] = useState([]), [ll, setLl] = useState(null), [res, setRes] = useState([]), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const [q, setQ] = useState(''), [places, setPlaces] = useState([]), [note, setNote] = useState('')
  const geo = () => navigator.geolocation.getCurrentPosition((p) => setLl([p.coords.latitude, p.coords.longitude]), () => setErr(t.geoFail))
  useEffect(() => {
    if (q.trim().length < 2) { setPlaces([]); return }
    const ctrl = new AbortController()
    const t = setTimeout(async () => {
      try {
        const r = await fetch(`/api/v1/geo/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal })
        if (r.ok) setPlaces(await r.json())
      } catch (e) {
        if (e.name !== 'AbortError') setPlaces([])
      }
    }, 250)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [q])
  const pickPlace = (p) => { setLl([p.lat, p.lon]); setQ(p.name.split(', ')[0]); setPlaces([]) }
  const fixLocation = async (incident) => {
    if (!ll) { setErr(t.loc_required); return null }
    return api(`/incidents/${incident.id}/location`, { method: 'PATCH', json: { lat: ll[0], lon: ll[1] } })
  }
  const submit = async () => {
    setBusy(true); setErr(''); const done = []
    for (const f of files) {
      const fd = new FormData(); fd.append('file', f)
      if (ll) { fd.append('lat', ll[0]); fd.append('lon', ll[1]) }
      if (note.trim()) fd.append('location_note', note.trim())
      try {
        let row = await api('/incidents/report', { body: fd })
        if (row.lat == null && ll) row = await fixLocation(row)
        else if (row.lat == null) setErr(t.loc_after)
        done.push(row)
      } catch (e) { setErr(e.message); break }
    }
    setRes(done); setBusy(false); onDone?.()
  }
  return (
    <div className="space-y-4">
      <Card lift={false} className="space-y-2 p-4 text-sm text-muted-foreground">
        <p className="font-semibold text-foreground">{t.loc_how_title}</p>
        <p>{t.loc_how_body}</p>
      </Card>
      <motion.label whileHover={{ scale: 1.01 }} className="grid min-h-40 cursor-pointer place-items-center gap-1 rounded-2xl border-2 border-dashed border-border/60 bg-muted p-6 text-center focus-within:ring-2 focus-within:ring-ring">
        <input type="file" multiple={multiple} accept="image/*,video/*" className="sr-only" onChange={(e) => setFiles([...e.target.files])} />
        <span className="font-semibold">{t.upload}</span>
        <span className="text-sm text-muted-foreground">{files.length ? files.map((f) => f.name).join(', ') : t.analyze_p}</span>
      </motion.label>
      <div className="space-y-2">
        <label className="block space-y-1"><span className="text-sm font-semibold">{t.loc_search}</span>
          <input className="field" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t.search_ph} /></label>
        {places.length > 0 && (
          <ul className="max-h-40 overflow-auto rounded-xl border border-border/50 bg-card text-sm">
            {places.map((p, k) => <li key={k}><button type="button" className="min-h-11 w-full cursor-pointer px-3 py-2 text-left hover:bg-muted" onClick={() => pickPlace(p)}>{p.name}</button></li>)}
          </ul>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Btn v="ghost" onClick={geo}>{t.geo}</Btn>
          {ll && <span className="font-mono text-sm">{ll[0].toFixed(6)}, {ll[1].toFixed(6)}</span>}
        </div>
        <label className="block space-y-1"><span className="text-sm font-semibold">{t.loc_note}</span>
          <input className="field" value={note} onChange={(e) => setNote(e.target.value)} placeholder={t.loc_note_ph} /></label>
      </div>
      <Btn shimmer className="w-full" disabled={busy || !files.length} onClick={submit}>{busy ? t.wait : t.send}</Btn>
      {err && <p role="alert" className="text-destructive">{err}</p>}
      {res.map((r) => <Incident key={r.id} i={r} t={t} canPdf={canPdf} />)}
    </div>
  )
}
