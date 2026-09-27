import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { Ambulance, Atom, Baby, Building2, Cross, Droplets, Factory, Flame, Hospital, Landmark, Loader2, Pill, Plane, Radiation, School, Shield, TriangleAlert, Waves, Wind, Zap } from 'lucide-react'
import { api } from '../api'
import { Card, spring } from '../ui'

const KIND_META = {
  nuclear:      { icon: Atom,          label: "АЭС",          color: "text-destructive",   crit: 5 },
  hydro_dam:    { icon: Waves,         label: "ГЭС",          color: "text-destructive",   crit: 5 },
  refinery:     { icon: Factory,       label: "НПЗ",          color: "text-destructive",   crit: 5 },
  chemical:     { icon: Radiation,     label: "Химзавод",     color: "text-destructive",   crit: 4 },
  hospital:     { icon: Hospital,      label: "Больница",     color: "text-tomato",        crit: 4 },
  fire_station: { icon: Flame,         label: "Пожарная часть", color: "text-tomato",   crit: 4 },
  airport:      { icon: Plane,         label: "Аэродром",     color: "text-orange-500",    crit: 4 },
  power_plant:  { icon: Zap,           label: "Электростанция", color: "text-orange-500", crit: 4 },
  police:       { icon: Shield,        label: "Полиция",      color: "text-yellow-500",    crit: 3 },
  substation:   { icon: Zap,           label: "Подстанция",   color: "text-yellow-500",    crit: 3 },
  helipad:      { icon: Cross,         label: "Вертодром",    color: "text-yellow-500",    crit: 3 },
  shelter:      { icon: Landmark,      label: "Укрытие",      color: "text-yellow-500",    crit: 3 },
  ambulance:    { icon: Ambulance,     label: "Скорая",       color: "text-yellow-500",    crit: 3 },
  government:   { icon: Landmark,      label: "Администрация", color: "text-green-600",  crit: 3 },
  clinic:       { icon: Hospital,      label: "Поликлиника",  color: "text-green-600",    crit: 2 },
  school:       { icon: School,        label: "Школа",        color: "text-green-600",    crit: 2 },
  kindergarten: { icon: Baby,          label: "Детсад",       color: "text-green-600",    crit: 2 },
  water:        { icon: Droplets,      label: "Водозабор",    color: "text-green-600",    crit: 2 },
  industrial:   { icon: Factory,       label: "Промзона",     color: "text-green-600",    crit: 2 },
  pharmacy:     { icon: Pill,          label: "Аптека",       color: "text-muted-foreground", crit: 1 },
  other:        { icon: Building2,     label: "Другое",       color: "text-muted-foreground", crit: 1 },
}



export default function CityInfoPanel({ t, place, onClose, onFocus }) {
  const [data, setData] = useState(null), [err, setErr] = useState('')
  useEffect(() => {
    if (!place) { setData(null); return }
    setErr(''); setData(null)
    api(`/geo/infrastructure?lat=${place.lat}&lon=${place.lon}`).then(setData).catch((e) => setErr(e.message))
  }, [place])
  if (!place) return null
  const title = place.name.split(', ')[0]
  return (
    <motion.aside initial={{ x: '1rem', opacity: 0 }} animate={{ x: 0, opacity: 1 }} exit={{ x: '1rem', opacity: 0 }} transition={spring}
      className="flex min-h-0 flex-col gap-3 overflow-hidden border-l border-border/50 bg-card/95 p-4 backdrop-blur">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{t.city_panel}</p>
          <h2 className="truncate text-lg font-extrabold">{title}</h2>
          <p className="truncate text-xs text-muted-foreground">{place.name.split(', ').slice(1).join(', ')}</p>
        </div>
        <button type="button" onClick={onClose} className="min-h-11 shrink-0 cursor-pointer rounded-full px-3 text-sm font-semibold text-muted-foreground hover:bg-muted">{t.close}</button>
      </div>
      {!data && !err && <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" aria-hidden />{t.city_loading}</div>}
      {err && <p role="alert" className="text-sm text-destructive">{err}</p>}
      {data && (
        <>
          {data.region && <p className="text-sm text-muted-foreground">{data.region}</p>}
          {data.weather && data.weather.wind_speed_ms !== null ? (
            <Card lift={false} className="flex flex-wrap gap-x-4 gap-y-1 p-3 font-mono text-xs">
              <span className="inline-flex items-center gap-1"><Wind className="h-3.5 w-3.5" aria-hidden />{data.weather.wind_speed_ms} m/s · {data.weather.wind_dir_deg}°</span>
              <span>{data.weather.temp_c}°C · {data.weather.humidity_pct}%</span>
            </Card>
          ) : <p className="text-xs text-muted-foreground">{t.wx_off}</p>}
          <div className="min-h-0 flex-1 overflow-y-auto">
            <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold"><Building2 className="h-4 w-4" aria-hidden />{t.city_infra}</h3>
            {data.max_criticality >= 4 && (
              <div role="alert" className="mb-3 rounded-xl border border-destructive/60 bg-red-500/10 p-3">
                <p className="flex items-center gap-2 font-semibold text-destructive">
                  <TriangleAlert className="h-4 w-4" aria-hidden />
                  В зоне — критический объект уровня {data.max_criticality}
                </p>
              </div>
            )}
            {data.groups && Object.keys(data.groups).length > 0 ? (
              <div className="space-y-3">
                {Object.entries(data.groups)
                  .sort(([a], [b]) => (KIND_META[b]?.crit ?? 0) - (KIND_META[a]?.crit ?? 0))
                  .map(([kind, items]) => {
                    const meta = KIND_META[kind] ?? { icon: Building2, label: kind, color: "", crit: 0 }
                    const Icon = meta.icon
                    return (
                      <details key={kind} className="rounded-xl border border-border/50 bg-card" open={meta.crit >= 4}>
                        <summary className="flex cursor-pointer items-center gap-2 p-3">
                          <Icon className={`h-4 w-4 ${meta.color}`} aria-hidden />
                          <span className="font-semibold">{meta.label}</span>
                          <span className="ml-auto font-mono text-xs">{items.length}</span>
                        </summary>
                        <ul className="border-t border-border/30 p-2 text-sm">
                          {items.map((f, i) => (
                            <li
                              key={i}
                              onClick={() => onFocus?.(f.lat, f.lon, 16)}
                              className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-muted"
                              title="Показать на карте"
                            >
                              <span className="truncate">{f.name}</span>
                              <span className="ml-auto font-mono text-xs text-muted-foreground">{f.distance_km} км</span>
                              {f.beds && <span className="font-mono text-xs">🛏 {f.beds}</span>}
                              {f.emergency && <span className="text-red-500" title="Круглосуточно">●</span>}
                              {f.phone && (
                                <a href={`tel:${f.phone}`} onClick={(e) => e.stopPropagation()} className="font-mono text-xs underline">
                                  {f.phone}
                                </a>
                              )}
                            </li>
                          ))}
                        </ul>
                      </details>
                    )
                  })}
              </div>
            ) : data.source === 'none' ? (
              <p role="alert" className="text-sm text-destructive">{t.city_infra_offline}</p>
            ) : (
              <p className="text-sm text-muted-foreground">{t.city_infra_empty}</p>
            )}
            <p className="mt-3 text-xs text-muted-foreground">
              {data.source === 'osm' ? t.src_osm_infra :
               data.source === 'partial' ? t.infra_partial :
               t.city_infra_empty}
            </p>
          </div>
        </>
      )}
    </motion.aside>
  )
}
