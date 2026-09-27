import { useCallback, useEffect, useState } from 'react'
import { AnimatePresence, motion, useDragControls } from 'framer-motion'
import { Check, Flame, MapPinOff, Trash2, X } from 'lucide-react'
import { api } from './api'
import RuMap from './RuMap'
import CityInfoPanel from './components/CityInfoPanel'
import ManualIncidentForm from './components/ManualIncidentForm'
import { Badge, Btn, Card, Skeleton, Tabs, spring, useMedia } from './ui'
import { Coord, FixLocation, Incident, Report, pct } from './parts'

const Empty = ({ t }) => (
  <div className="grid place-items-center gap-2 rounded-2xl border border-dashed border-border/60 p-8 text-center text-muted-foreground"><MapPinOff className="h-6 w-6" aria-hidden />{t.empty}</div>
)

function Sheet({ open, setOpen, title, children }) {
  const controls = useDragControls()
  return (
    <motion.section initial={false} animate={{ y: open ? '0%' : '82%' }} transition={spring} drag="y" dragControls={controls} dragListener={false} dragConstraints={{ top: 0, bottom: 0 }} dragElastic={0.2}
      onDragEnd={(_, info) => info.offset.y !== 0 && setOpen(info.offset.y < 0)} className="absolute inset-x-0 bottom-0 z-20 flex h-sheet flex-col rounded-t-3xl border border-border/50 bg-card shadow-soft">
      <button aria-expanded={open} onPointerDown={(e) => controls.start(e)} onClick={() => setOpen(!open)} className="grid min-h-16 w-full cursor-pointer touch-none place-items-center gap-1 px-4 pt-2">
        <motion.span aria-hidden whileTap={{ scaleX: 1.3 }} className="h-1.5 w-12 rounded-full bg-gradient-to-r from-border/40 via-border to-border/40" /><span className="text-sm font-semibold">{title}</span>
      </button>
      <div className="min-h-0 flex-1 overflow-y-auto p-4 pb-safe">{children}</div>
    </motion.section>
  )
}

function ReviewCard({ i, t, busy, review, setSel }) {
  return (
    <Card lift={false} className="space-y-3 p-4">
      {i.annotated_path && <img src={`/uploads/${i.annotated_path}`} alt="" loading="lazy" className="aspect-video w-full rounded-xl object-cover" />}
      <div className="flex items-center justify-between"><Badge status={i.status}>{t['s_' + i.status]}</Badge><span className="font-mono text-sm">{i.source_ai} {pct(i.confidence)}</span></div>
      {i.lat != null ? <Coord i={i} t={t} /> : <p className="text-sm text-destructive">{t.no_gps}</p>}
      <div className="flex flex-wrap gap-2">
        <Btn disabled={busy === i.id} onClick={() => review(i, 'confirm')}><Check className="h-4 w-4" aria-hidden />{t.confirm}</Btn>
        <Btn v="warn" disabled={busy === i.id} onClick={() => review(i, 'false_alarm')}><X className="h-4 w-4" aria-hidden />{t.falseAlarm}</Btn>
        {i.lat != null && <Btn v="ghost" onClick={() => setSel(i.id)}>{t.open_map}</Btn>}
      </div>
    </Card>
  )
}

function List({ items, sel, setSel, t, onDelete }) {
  if (items === null) return <div className="space-y-3"><Skeleton /><Skeleton /></div>
  if (!items.length) return <Empty t={t} />
  return (
    <ul className="space-y-2">
      {items.map((i) => (
        <li key={i.id} className={`rounded-2xl border p-3 ${sel === i.id ? 'border-ring bg-muted' : 'border-border/50 bg-card'}`}>
          <motion.button aria-pressed={sel === i.id} whileHover={{ x: '0.25rem' }} whileTap={{ scale: 0.98 }} onClick={() => setSel(i.id)}
            className="grid min-h-11 w-full cursor-pointer gap-1 text-left">
            <span className="flex items-center justify-between gap-2"><Badge status={i.status}>{t['s_' + i.status]}</Badge><span className="font-mono text-xs">#{i.id}</span></span>
            <span className="truncate text-sm">{i.region_name || '—'}</span>
            <span className="font-mono text-xs text-muted-foreground">{i.lat != null ? `${i.lat.toFixed(6)}, ${i.lon.toFixed(6)}` : t.no_gps}</span>
          </motion.button>
          {onDelete && <Btn v="ghost" className="mt-2 text-destructive" onClick={() => onDelete(i)}><Trash2 className="h-4 w-4" aria-hidden />{t.delete}</Btn>}
        </li>
      ))}
    </ul>
  )
}

function ForesterPanel({ t, items, sel, setSel, reload, pick, role }) {
  const [tab, setTab] = useState('queue'), [busy, setBusy] = useState(0)
  const queue = (items || []).filter((i) => i.status === 'PENDING_VERIFICATION')
  const active = (items || []).filter((i) => i.lat != null && i.status !== 'FALSE_ALARM')
  const all = items || []
  const review = async (i, verdict) => { setBusy(i.id); try { await api(`/incidents/${i.id}/review`, { json: { verdict } }); await reload() } finally { setBusy(0) } }
  const del = async (i) => { if (!confirm(t.delete_confirm)) return; await api(`/incidents/${i.id}`, { method: 'DELETE' }); await reload() }
  return (
    <div className="space-y-4">
      <Tabs id="f-tabs" value={tab} onChange={setTab} tabs={[['queue', `${t.tab_queue} (${queue.length})`], ['maplist', `${t.tab_map} (${active.length})`], ['all', `${t.tab_all} (${all.length})`], ['manual', t.tab_new], ['upload', t.tab_upload]]} />
      {tab === 'upload' && <Report t={t} multiple onDone={reload} />}
      {tab === 'manual' && <ManualIncidentForm t={t} pick={pick} role={role} onDone={reload} />}
      {tab === 'maplist' && <List items={active} sel={sel} setSel={setSel} t={t} onDelete={del} />}
      {tab === 'all' && <List items={all} sel={sel} setSel={setSel} t={t} onDelete={del} />}
      {tab === 'queue' && (!queue.length ? <Empty t={t} /> : queue.map((i) => (
        <div key={i.id} className="space-y-2">
          <ReviewCard i={i} t={t} busy={busy} review={review} setSel={setSel} />
          {i.lat == null && <FixLocation i={i} t={t} onFixed={reload} />}
        </div>
      )))}
    </div>
  )
}

function MchsPanel({ t, items, setSel, pick, reload, role }) {
  const [tab, setTab] = useState('hot'), [busy, setBusy] = useState(0)
  const hot = (items || []).filter((i) => ['CRITICAL_ALERT', 'CONFIRMED'].includes(i.status))
  const queue = (items || []).filter((i) => i.status === 'PENDING_VERIFICATION')
  const del = async (i) => { if (!confirm(t.delete_confirm)) return; await api(`/incidents/${i.id}`, { method: 'DELETE' }); await reload() }
  const review = async (i, verdict) => { setBusy(i.id); try { await api(`/incidents/${i.id}/review`, { json: { verdict } }); await reload() } finally { setBusy(0) } }
  return (
    <div className="space-y-4">
      <Tabs id="m-tabs" value={tab} onChange={setTab} tabs={[['hot', `${t.tab_hot} (${hot.length})`], ['queue', `${t.tab_review} (${queue.length})`], ['new', t.tab_new]]} />
      {tab === 'new' ? (
        <ManualIncidentForm t={t} pick={pick} role={role} onDone={reload} />
      ) : tab === 'queue' ? (items === null ? <Skeleton /> : !queue.length ? <Empty t={t} /> : queue.map((i) => (
        <div key={i.id} className="space-y-2">
          <p className="text-xs text-muted-foreground">{t.manual_forester_note}</p>
          <ReviewCard i={i} t={t} busy={busy} review={review} setSel={setSel} />
          {i.lat == null && <FixLocation i={i} t={t} onFixed={reload} />}
        </div>
      ))) : items === null ? <Skeleton /> : !hot.length ? <Empty t={t} /> : hot.map((i) => (
        <div key={i.id} className="space-y-2">
          <Incident i={i} t={t} canPdf onFocus={setSel} onDelete={() => del(i)} />
        </div>
      ))}
    </div>
  )
}

export default function Workspace({ t, role, dark, lang, tick, go }) {
  const staff = role === 'forester' || role === 'mchs'
  const wide = useMedia('(min-width: 64rem)')
  const xl = useMedia('(min-width: 80rem)')
  const [items, setItems] = useState(null), [sel, setSel] = useState(null), [pick, setPick] = useState(null), [open, setOpen] = useState(false)
  const [city, setCity] = useState(null), [infra, setInfra] = useState([]), [cityData, setCityData] = useState(null)
  // Item 2: the forester feed keeps near-misses (CLEAR) and no-GPS rows; MCHS only sees actionable fires.
  const reload = useCallback(() => {
    const path = role === 'forester' ? '/incidents?include_clear=1' : staff ? '/incidents' : '/incidents/public'
    return api(path).then(setItems).catch(() => setItems((x) => x ?? []))
  }, [staff, role])
  useEffect(() => { reload(); const id = setInterval(reload, 30000); return () => clearInterval(id) }, [reload, tick])
useEffect(() => {
  if (!city) { setInfra([]); setCityData(null); return }
  api(`/geo/infrastructure?lat=${city.lat}&lon=${city.lon}`)
    .then((d) => { setInfra(d.facilities || []); setCityData(d) })
    .catch(() => { setInfra([]); setCityData(null) })
}, [city])
  const [focus, setFocus] = useState(null)  // { lat, lon, zoom }
  useEffect(() => {
    const i = (items || []).find((x) => x.id === sel)
    if (i?.lat != null) setFocus({ lat: i.lat, lon: i.lon, zoom: 11 })
  }, [sel, items])
  const select = (id) => { setSel(id); setOpen(false) }
  const canPick = role === 'mchs' || role === 'forester'
  const panel = role === 'mchs' ? <MchsPanel t={t} items={items} setSel={select} pick={pick} reload={reload} role={role} />
    : role === 'forester' ? <ForesterPanel t={t} items={items} sel={sel} setSel={select} reload={reload} pick={pick} role={role} />
    : (
      <div className="space-y-4">
        <Btn shimmer className="w-full" onClick={() => go('analyze')}><Flame className="h-4 w-4" aria-hidden />{t.report}</Btn>
        <h2 className="text-lg font-extrabold">{t.hot}{items ? ` (${items.length})` : ''}</h2>
        <List items={items} sel={sel} setSel={select} t={t} />
      </div>
    )
  const showCity = role === 'mchs' && xl && city
  return (
    <div className={`relative h-workspace md:h-workspace-md ${wide ? 'grid' : ''} ${showCity ? 'xl:grid-cols-[minmax(0,22rem)_minmax(0,1fr)_minmax(0,20rem)]' : 'lg:grid-cols-workspace'}`}>
      {wide && <aside className="min-h-0 space-y-4 overflow-y-auto border-r border-border/50 p-4">{panel}</aside>}
      <RuMap items={items || []} dark={dark} lang={lang} t={t} focus={focus} onPick={canPick ? setPick : undefined} picked={pick}
        onCitySelect={role === 'mchs' ? setCity : undefined} infra={role === 'mchs' ? infra : []} className="h-full rounded-none border-0" />
      <AnimatePresence>
        {showCity && (
          <CityInfoPanel
            t={t}
            place={city}
            data={cityData}
            onClose={() => setCity(null)}
            onFocus={(lat, lon, zoom = 16) => setFocus({ lat, lon, zoom })}
          />
        )}
      </AnimatePresence>
      {!wide && <Sheet open={open} setOpen={setOpen} title={t.peek}>{panel}</Sheet>}
    </div>
  )
}
