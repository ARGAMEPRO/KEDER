import { useState } from 'react'
import { Radio } from 'lucide-react'
import { api } from '../api'
import { Btn, Card, Tabs } from '../ui'
import MiniMap from './MiniMap'
import { Incident, Report } from '../parts'

function RtspForm({ t }) {
  const [url, setUrl] = useState('')
  const [point, setPoint] = useState(null)
  const [seconds, setSeconds] = useState(30)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [row, setRow] = useState(null)

  const submit = async () => {
    if (!point) { setErr(t.location_required); return }
    if (!/^rtsps?:|^https?:/.test(url.trim())) { setErr(t.rtsp_url_hint); return }
    setBusy(true); setErr('')
    try {
      const created = await api('/incidents/rtsp', {
        json: { url: url.trim(), lat: point[0], lon: point[1], seconds: Number(seconds) },
        timeoutMs: 300000,
      })
      setRow(created)
    } catch (e) { setErr(e.message) } finally { setBusy(false) }
  }

  return (
    <div className="space-y-4">
      <Card lift={false} className="space-y-2 p-4 text-sm text-muted-foreground">
        <p className="font-semibold text-foreground">{t.rtsp_title}</p>
        <p>{t.rtsp_body}</p>
      </Card>

      <label className="block space-y-1">
        <span className="text-sm font-semibold">{t.rtsp_url}</span>
        <input className="field font-mono" placeholder="rtsp://camera.example.ru/stream"
          value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>

      <div className="space-y-1">
        <span className="text-sm font-semibold">{t.step_place}</span>
        <MiniMap t={t} value={point} onChange={setPoint} />
        <p className="font-mono text-xs text-muted-foreground">
          {point ? `${point[0].toFixed(6)}, ${point[1].toFixed(6)}` : t.pick_hint}
        </p>
      </div>

      <label className="block space-y-1">
        <span className="text-sm font-semibold">{t.rtsp_seconds}</span>
        <input className="field w-32 font-mono" type="number" min="1" max="120"
          value={seconds} onChange={(e) => setSeconds(e.target.value)} />
      </label>

      <Btn className="w-full" disabled={busy} onClick={submit}>
        <Radio className="h-4 w-4" aria-hidden />{busy ? t.wait : t.rtsp_scan}
      </Btn>
      {err && <p role="alert" className="text-sm text-destructive">{err}</p>}
      {row && <Incident i={row} t={t} />}
    </div>
  )
}

/** Analysis page: files for everyone, live streams for staff. */
export default function AnalysisPage({ t, role, canPdf }) {
  const staff = role === 'forester' || role === 'mchs'
  const [mode, setMode] = useState('file')
  return (
    <div className="space-y-4">
      {staff && (
        <Tabs id="an-tabs" value={mode} onChange={setMode}
          tabs={[['file', t.analysis_file], ['rtsp', t.analysis_rtsp]]} />
      )}
      {staff && mode === 'rtsp' ? <RtspForm t={t} /> : <Report t={t} multiple canPdf={canPdf} />}
    </div>
  )
}
