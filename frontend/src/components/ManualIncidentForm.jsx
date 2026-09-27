import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { AlertTriangle, Camera, Check, MapPin, Sparkles } from 'lucide-react'
import { api } from '../api'
import { Btn, Card, spring } from '../ui'

/** Step-style manual incident form (21st.dev-inspired glass card flow). */
export default function ManualIncidentForm({ t, pick, role, onDone, onNeedPick }) {
  const [file, setFile] = useState(null), [preview, setPreview] = useState(''), [busy, setBusy] = useState(false), [err, setErr] = useState('')
  const [verdict, setVerdict] = useState('confirm')
  const forester = role === 'forester', mchs = role === 'mchs'
  const onFile = (f) => {
    setFile(f)
    if (preview) URL.revokeObjectURL(preview)
    setPreview(f ? URL.createObjectURL(f) : '')
  }
  const submit = async () => {
    if (!pick) { onNeedPick?.(); setErr(t.pick_hint); return }
    if (forester && !file) { setErr(t.manual_photo_req); return }
    setBusy(true); setErr('')
    try {
      if (forester || mchs || file) {
        const fd = new FormData()
        fd.append('lat', pick[0]); fd.append('lon', pick[1])
        if (file) fd.append('file', file)
        if (mchs) fd.append('verdict', verdict)
        await api('/incidents/manual-photo', { body: fd })
      } else {
        await api('/incidents/manual', { json: { lat: pick[0], lon: pick[1] } })
      }
      onDone?.()
    } catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const step = (n) => (
    <span className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full bg-primary/10 font-mono text-xs font-bold text-primary ring-1 ring-primary/20" aria-hidden>{n}</span>
  )
  return (
    <Card lift={false} className="space-y-4 p-4">
      <div className="flex items-center gap-3">
        <span className="grid h-11 w-11 shrink-0 place-items-center rounded-2xl bg-gradient-to-br from-accent/80 to-muted ring-1 ring-border/40"><Sparkles className="h-5 w-5 text-primary" aria-hidden /></span>
        <div className="min-w-0"><h2 className="text-lg font-extrabold leading-tight">{t.manual}</h2><p className="text-sm text-muted-foreground">{t.manual_sub}</p></div>
      </div>
      <ol className="space-y-3 text-sm">
        <li className="flex gap-3 rounded-2xl border border-border/50 bg-muted/40 p-3">
          {step(1)}
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-2 font-semibold"><MapPin className="h-4 w-4 text-primary" aria-hidden />{t.step_place}</p>
            {pick ? <p className="mt-0.5 font-mono text-xs">{pick[0].toFixed(6)}, {pick[1].toFixed(6)}</p>
              : <p className="mt-0.5 text-muted-foreground">{t.pick_hint}</p>}
          </div>
          {!pick && <Btn v="ghost" onClick={() => onNeedPick?.()}>{t.pick_action}</Btn>}
        </li>
        <li className="flex gap-3">
          {step(2)}
          <div className="min-w-0 flex-1 space-y-2">
            <motion.label whileHover={{ scale: 1.005 }} className="grid min-h-32 cursor-pointer gap-2 rounded-2xl border-2 border-dashed border-border/60 bg-card p-4 text-center focus-within:ring-2 focus-within:ring-ring">
              <input type="file" accept="image/*" className="sr-only" onChange={(e) => onFile(e.target.files?.[0] || null)} />
              <Camera className="mx-auto h-6 w-6 text-muted-foreground" aria-hidden />
              <span className="font-semibold">{forester ? t.manual_photo_req : t.manual_photo_opt}</span>
              <span className="text-xs text-muted-foreground">{file?.name || t.manual_drop}</span>
            </motion.label>
            <AnimatePresence>
              {preview && (
                <motion.img key={preview} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} src={preview} alt="" className="aspect-video w-full rounded-xl object-cover" />
              )}
            </AnimatePresence>
          </div>
        </li>
        {mchs && (
          <li className="flex gap-3">
            {step(3)}
            <div className="min-w-0 flex-1 space-y-2">
              <p className="font-semibold">{t.verdict_title}</p>
              <p className="text-xs text-muted-foreground">{t.verdict_hint}</p>
              <div className="flex gap-2" role="radiogroup" aria-label={t.verdict_title}>
                {[['confirm', t.verdict_confirm, Check], ['critical', t.verdict_critical, AlertTriangle]].map(([v, label, Icon]) => (
                  <button key={v} type="button" role="radio" aria-checked={verdict === v} onClick={() => setVerdict(v)}
                    className={`flex min-h-11 flex-1 cursor-pointer items-center justify-center gap-2 rounded-xl border px-3 text-sm font-semibold ${verdict === v ? (v === 'critical' ? 'border-tomato bg-tomato/10 text-tomato' : 'border-ring bg-muted') : 'border-border/60 text-muted-foreground hover:bg-muted'}`}>
                    <Icon className="h-4 w-4" aria-hidden />{label}
                  </button>
                ))}
              </div>
            </div>
          </li>
        )}
      </ol>
      <Btn v="warn" className="w-full" disabled={!pick || busy || (forester && !file)} onClick={submit}>{busy ? t.wait : t.create}</Btn>
      {err && <p role="alert" className="text-sm text-destructive">{err}</p>}
      {forester && <p className="text-xs text-muted-foreground">{t.manual_forester_note}</p>}
    </Card>
  )
}

export { spring }
