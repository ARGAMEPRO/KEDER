import { useCallback, useEffect, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'
import { feature } from 'topojson-client'
import world from 'world-atlas/countries-50m.json'
import disputed from './disputed.json'
import { AnimatePresence, animate, motion } from 'framer-motion'
import { Layers, Search } from 'lucide-react'
import { api } from './api'
import { StatusDot } from './ui'

const RU = feature(world, world.objects.countries).features.find((f) => String(f.id) === '643')
const area = (r) => r.reduce((s, [x1, y1], i) => { const [x2, y2] = r[(i + 1) % r.length]; return s + (x1 * y2 - x2 * y1) }, 0) / 2
// Разворачиваем долготу непрерывно: убираем скачки ±360 на антимеридиане,
// которые иначе превращаются в «шов» через всю карту и ломают заливку.
const unwrap = (r) => {
  if (!r.length) return r
  const out = [[r[0][0], r[0][1]]]
  let off = 0
  for (let i = 1; i < r.length; i++) {
    const d = r[i][0] - r[i - 1][0]
    if (d > 180) off -= 360
    else if (d < -180) off += 360
    out.push([r[i][0] + off, r[i][1]])
  }
  return out
}
// Отсечение многоугольника по вертикальной линии x = X (Sutherland-Hodgman).
const clipHalf = (r, X, keepRight) => {
  const inside = ([x]) => (keepRight ? x >= X : x <= X)
  const out = []
  for (let i = 0; i < r.length; i++) {
    const cur = r[i], prev = r[(i - 1 + r.length) % r.length]
    const ci = inside(cur), pi = inside(prev)
    const cut = () => [X, prev[1] + ((X - prev[0]) / (cur[0] - prev[0])) * (cur[1] - prev[1])]
    if (ci) { if (!pi) out.push(cut()); out.push(cur) }
    else if (pi) out.push(cut())
  }
  return out
}
const clipWindow = (r, lo, hi) => {
  let a = clipHalf(r, lo, true)
  if (a.length < 3) return []
  a = clipHalf(a, hi, false)
  return a.length >= 3 ? a : []
}
// Внешнее кольцо -> дырка(и) в маске. Кольцо, пересекающее антимеридиан, разрезаем по окнам
// долготы и возвращаем каждую часть обратно в [-180, 180]; обход приводим к CW (дырка).
const hole = (ring) => {
  const u = unwrap(ring)
  const pieces = []
  for (let k = -2; k <= 2; k++) {
    const lo = -180 + 360 * k, hi = 180 + 360 * k
    const c = clipWindow(u, lo, hi)
    if (c.length < 3) continue
    const s = c.map(([x, y]) => [x - 360 * k, y])
    const a = area(s)
    if (Math.abs(a) < 1e-9) continue
    // Ensure proper winding for holes (counterclockwise)
    const oriented = a > 0 ? s.reverse() : s
    pieces.push(oriented)
  }
  return pieces.filter(p => p.length >= 3)
}
const outerRings = (g) => { if (!g) return []; const polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates; return polys.map((p) => p[0]).filter(Boolean) }
// Спорные территории, которые показываем как российские (Крым и Севастополь уже входят в RU по Natural Earth)
const DISPUTED = disputed.features
// Контуры России и спорных территорий, разрезанные по антимеридиану (без «шва» через всю карту)
const RU_PIECES = outerRings(RU.geometry).flatMap(hole)
const DISPUTED_PIECES = DISPUTED.flatMap((f) => outerRings(f.geometry).flatMap(hole))
const toFC = (rings) => ({ type: 'FeatureCollection', features: rings.map((r) => ({ type: 'Feature', geometry: { type: 'Polygon', coordinates: [r] }, properties: {} })) })
const RU_FC = toFC(RU_PIECES)
const DISPUTED_FC = toFC(DISPUTED_PIECES)
// Маска = мир минус Россия минус спорные территории; контуры переводим в дырки (обход по часовой стрелке)
const createMask = () => {
  try {
    const world = [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]]]
    // Only include valid pieces and ensure they form proper holes
    const validPieces = [...RU_PIECES, ...DISPUTED_PIECES].filter(p => p && p.length >= 3)
    if (validPieces.length === 0) {
      console.warn('[kedr] No valid pieces for mask')
      return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [world] } }
    }
    // Create a simpler mask without holes to avoid rendering issues
    return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [world] } }
  } catch (e) {
    console.error('[kedr] Mask creation error:', e)
    return { type: 'Feature', geometry: { type: 'Polygon', coordinates: [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]]] } }
  }
}
const MASK = createMask()
const COLOR = { CRITICAL_ALERT: '#D52518', CONFIRMED: '#F96015', PENDING_VERIFICATION: '#FFC926' }
const EMPTY = { type: 'FeatureCollection', features: [] }
const STYLE = {
  light: 'https://tiles.openfreemap.org/styles/positron',
  dark: 'https://tiles.openfreemap.org/styles/dark',
}
// Спутниковый базовый слой: Esri World Imagery (растровые тайлы, без API-ключа)
const SAT = {
  version: 8,
  sources: {
    sat: {
      type: 'raster',
      tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256,
      maxzoom: 19,
      attribution: 'Imagery © Esri, Maxar, Earthstar Geographics',
    },
  },
  layers: [{ id: 'sat', type: 'raster', source: 'sat' }],
}
// Палитра объектов критической инфраструктуры (для точек МЧС на карте)
const INFRA_COLOR = {
  nuclear: '#8B0000', hydro_dam: '#8B0000', refinery: '#8B0000',
  chemical: '#D52518', hospital: '#B23A48', fire_station: '#D52518',
  airport: '#F96015', power_plant: '#F96015',
  police: '#2A4B8D', substation: '#2A4B8D', helipad: '#2A4B8D', shelter: '#2A4B8D',
  ambulance: '#3E7CB1', government: '#4B3F72',
  clinic: '#5FA8D3', school: '#5FA8D3', kindergarten: '#5FA8D3', water: '#1D7A9E', industrial: '#5A5A5A',
  pharmacy: '#2E8B57', other: '#31572C',
}
const INFRA_CRITICALITY = {
  nuclear: 5, hydro_dam: 5, refinery: 5,
  chemical: 4, hospital: 4, fire_station: 4, airport: 4, power_plant: 4,
  police: 3, substation: 3, helipad: 3, shelter: 3, ambulance: 3, government: 3,
  clinic: 2, school: 2, kindergarten: 2, water: 2, industrial: 2,
  pharmacy: 1, other: 1,
}
const INFRA_MATCH = ['match', ['get', 'kind'], ...Object.entries(INFRA_COLOR).flatMap(([k, c]) => [k, c]), '#31572C']

// Color scale for cellular automata intensity (0-1) - step function format: [step, input, output0, stop1, output1, stop2, output2, ...]
const INTENSITY_MATCH = ['step', ['get', 'intensity'], '#FFA07A', 0.2, '#FF6347', 0.4, '#FF4500', 0.6, '#DC143C', 0.8, '#8B0000']

const RING = { CRITICAL_ALERT: 'bg-tomato ring-tomato/30', CONFIRMED: 'bg-carrot ring-carrot/30', PENDING_VERIFICATION: 'bg-sunshine ring-sunshine/40' }
const dot = (status) => {
  const wrap = document.createElement('div'), halo = document.createElement('span'), core = document.createElement('span')
  const tone = RING[status] || 'bg-grass ring-grass/30'
  wrap.className = 'grid place-items-center'
  halo.className = `absolute h-5 w-5 rounded-full ${tone.split(' ')[0]}`
  core.className = `block h-5 w-5 cursor-pointer rounded-full border-2 border-white shadow-soft ring-4 ${tone}`
  wrap.append(halo, core)
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) animate(halo, { scale: [1, 2.4], opacity: [0.5, 0] }, { duration: status === 'CRITICAL_ALERT' ? 1.4 : 2.4, repeat: Infinity, ease: 'easeOut' })
  return wrap
}
const popup = (i) => { const d = document.createElement('div'); d.textContent = `#${i.id}  ${i.lat.toFixed(6)}, ${i.lon.toFixed(6)}`; return d }
const infraPopup = (p, t) => {
  const d = document.createElement('div')
  d.className = 'space-y-0.5 text-sm'
  const name = document.createElement('p'); name.className = 'font-semibold'; name.textContent = p.name || t?.['infra_' + p.kind] || p.kind
  const kind = document.createElement('p'); kind.className = 'text-xs opacity-70'
  kind.textContent = `${t?.['infra_' + p.kind] || p.kind}${p.distance_km != null ? ` · ${p.distance_km} ${t?.dist || 'km'}` : ''}`
  if (p.criticality && p.criticality >= 4) {
    const crit = document.createElement('p'); crit.className = 'text-xs text-red-500 font-semibold'
    crit.textContent = 'Критический объект'
    d.append(crit)
  }
  d.append(name, kind)
  return d
}

function addCustomLayers(m, maskColor) {
  try {
    if (!m.getSource('mask')) {
      console.log('[kedr] Adding custom layers')
      // Add mask layer for non-Russia areas
      m.addSource('mask', { type: 'geojson', data: MASK })
      m.addLayer({ id: 'mask', type: 'fill', source: 'mask', paint: { 'fill-color': maskColor, 'fill-opacity': 0.3 } })

      // Add Russia border lines
      m.addSource('ru', { type: 'geojson', data: RU_FC })
      m.addLayer({ id: 'ru-line', type: 'line', source: 'ru', paint: { 'line-color': '#31572C', 'line-width': 1.4 } })

      // Add disputed territories
      m.addSource('disputed', { type: 'geojson', data: DISPUTED_FC })
      m.addLayer({ id: 'disputed-line', type: 'line', source: 'disputed', paint: { 'line-color': '#31572C', 'line-width': 1.4 } })

      // Add hazard zones
      m.addSource('haz', { type: 'geojson', data: EMPTY })
      m.addLayer({ id: 'haz-fill', type: 'fill', source: 'haz', paint: { 'fill-color': '#D52518', 'fill-opacity': 0.22 } })
      m.addLayer({ id: 'haz-line', type: 'line', source: 'haz', paint: { 'line-color': '#D52518', 'line-width': 1.5 } })

      // Add cellular automata cells layer (as squares)
      m.addSource('cells', { type: 'geojson', data: EMPTY })
      m.addLayer({ id: 'cells', type: 'fill', source: 'cells', paint: {
        'fill-color': INTENSITY_MATCH,
        'fill-opacity': 0.7,
      }})
      m.addLayer({ id: 'cells-outline', type: 'line', source: 'cells', paint: {
        'line-color': '#FFFFFF',
        'line-width': 1,
        'line-opacity': 0.5
      }})

      // Add infrastructure points
      m.addSource('infra', { type: 'geojson', data: EMPTY })
      m.addLayer({ id: 'infra', type: 'circle', source: 'infra', paint: { 'circle-radius': 6, 'circle-color': INFRA_MATCH, 'circle-stroke-width': 2, 'circle-stroke-color': '#FAF9F6' } })
    } else {
      m.setPaintProperty('mask', 'fill-color', maskColor)
    }
  } catch (e) {
    console.error('[kedr] Error adding custom layers:', e)
  }
}

export default function RuMap({ items = [], dark, lang = 'ru', onPick, picked, focus, className = 'h-map', t, onCitySelect, infra = [] }) {
  const el = useRef(), map = useRef(), marks = useRef([]), pin = useRef(), cur = useRef(), pickRef = useRef(onPick)
  const cityRef = useRef(onCitySelect), tRef = useRef(t), ipop = useRef(null)
  const [failed, setFailed] = useState(false), [ready, setReady] = useState(false), [q, setQ] = useState(''), [res, setRes] = useState([])
  const [base, setBase] = useState('map')
  pickRef.current = onPick
  cityRef.current = onCitySelect
  tRef.current = t

  const maskColor = base === 'sat' ? '#141414' : dark ? '#2a2a2a' : '#DDE5C8'

  const bindMap = useCallback((m) => {
    m.on('click', (e) => {
      if (m.getLayer('infra')) {
        const hits = m.queryRenderedFeatures(e.point, { layers: ['infra'] })
        if (hits.length) {
          ipop.current?.remove()
          ipop.current = new maplibregl.Popup({ offset: 12, closeButton: true }).setLngLat(e.lngLat).setDOMContent(infraPopup(hits[0].properties, tRef.current)).addTo(m)
          return
        }
      }
      const p = e.lngLat.wrap(); pickRef.current?.([p.lat, p.lng])
    })
    m.on('mousemove', (e) => { const p = e.lngLat.wrap(); if (cur.current) cur.current.textContent = `${p.lat.toFixed(6)}, ${p.lng.toFixed(6)}` })
  }, [])

  useEffect(() => {
    setReady(false)
    let m
    try {
      console.log('[kedr] Initializing map with base:', base, 'dark:', dark)
      m = new maplibregl.Map({
        container: el.current,
        style: base === 'sat' ? SAT : dark ? STYLE.dark : STYLE.light,
        bounds: [[19, 41], [190, 78]],
        fitBoundsOptions: { padding: 10 },
        maxBounds: [[10, 35], [200, 85]],
        minZoom: 2,
        attributionControl: { compact: true },
      })
      console.log('[kedr] Map initialized successfully')
    } catch (e) { console.error('[kedr map] init', e); setFailed(true); return }
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
    m.on('error', (e) => console.error('[kedr map]', e.error || e))
    m.on('load', () => {
      console.log('[kedr] Map loaded, adding custom layers')
      try {
        addCustomLayers(m, maskColor)
        m.on('mouseenter', 'infra', () => { m.getCanvas().style.cursor = 'pointer' })
        m.on('mouseleave', 'infra', () => { m.getCanvas().style.cursor = '' })
        setReady(true)
      } catch (e) {
        console.error('[kedr] Error in map load handler:', e)
        setReady(true)  // Still set ready to prevent blocking
      }
    })
    bindMap(m)
    map.current = m
    return () => {
      try {
        m.remove()
      } catch (e) {
        console.error('[kedr] Error removing map:', e)
      }
    }
  }, [dark, bindMap, maskColor, base])

  useEffect(() => {
    if (!ready) return
    const m = map.current, f = lang === 'ru' ? 'name:ru' : 'name:latin'
    try {
      m.getStyle().layers.filter((l) => l.type === 'symbol' && JSON.stringify(l.layout?.['text-field'] ?? '').includes('name'))
        .forEach((l) => m.setLayoutProperty(l.id, 'text-field', ['coalesce', ['get', f], ['get', 'name']]))
    } catch (e) { console.warn('[kedr map] labels', e) }
  }, [lang, ready])

  useEffect(() => {
    if (!ready) return
    marks.current.forEach((x) => x.remove()); marks.current = []
    items.filter((i) => i.lat != null).forEach((i) => marks.current.push(
      new maplibregl.Marker({ element: dot(i.status) })
        .setLngLat([i.lon, i.lat]).setPopup(new maplibregl.Popup({ offset: 14 }).setDOMContent(popup(i))).addTo(map.current)))
    map.current.getSource('haz').setData({ type: 'FeatureCollection', features: items.filter((i) => i.hazard_geojson)
      .map((i) => (i.hazard_geojson.type === 'Feature' ? i.hazard_geojson : { type: 'Feature', geometry: i.hazard_geojson, properties: {} })) })
  }, [items, ready])

  useEffect(() => {
    if (!ready || !map.current.getSource('infra')) return
    map.current.getSource('infra').setData({
      type: 'FeatureCollection',
      features: (infra || []).map((f) => ({ type: 'Feature', geometry: { type: 'Point', coordinates: [f.lon, f.lat] }, properties: { kind: f.kind, name: f.name, distance_km: f.distance_km } })),
    })
  }, [infra, ready])

  useEffect(() => {
    if (!ready || !map.current.getSource('cells')) return
    const cellsFeatures = []
    items.forEach((i) => {
      if (i.hazard_geojson && i.hazard_geojson.properties && i.hazard_geojson.properties.cells) {
        i.hazard_geojson.properties.cells.forEach((cell) => {
          // Use polygon if available, otherwise fall back to point
          if (cell.polygon) {
            cellsFeatures.push({
              type: 'Feature',
              geometry: { type: 'Polygon', coordinates: [cell.polygon] },
              properties: { intensity: cell.intensity, fuel: cell.fuel }
            })
          } else {
            cellsFeatures.push({
              type: 'Feature',
              geometry: { type: 'Point', coordinates: [cell.lon, cell.lat] },
              properties: { intensity: cell.intensity, fuel: cell.fuel }
            })
          }
        })
      }
    })
    map.current.getSource('cells').setData({
      type: 'FeatureCollection',
      features: cellsFeatures
    })
  }, [items, ready])

  useEffect(() => {
    pin.current?.remove(); pin.current = null
    if (ready && picked) pin.current = new maplibregl.Marker({ color: '#FFC926' }).setLngLat([picked[1], picked[0]]).addTo(map.current)
  }, [picked, ready])

  useEffect(() => {
    if (q.trim().length < 2) { setRes([]); return }
    const ctrl = new AbortController()
    const t = setTimeout(async () => {
      try {
        const r = await fetch(`/api/v1/geo/search?q=${encodeURIComponent(q)}`, { signal: ctrl.signal })
        if (r.ok) setRes(await r.json())
      } catch (e) {
        if (e.name !== 'AbortError') setRes([])
      }
    }, 250)
    return () => { clearTimeout(t); ctrl.abort() }
  }, [q])

  useEffect(() => { if (ready && focus) map.current.flyTo({ center: [focus[1], focus[0]], zoom: 11 }) }, [focus, ready])

  const go = (r) => {
    console.log('[kedr] Going to location:', r)
    if (!map.current) {
      console.error('[kedr] Map not initialized')
      return
    }
    try {
      map.current.flyTo({ center: [r.lon, r.lat], zoom: 11 })
      setRes([]); setQ(r.name.split(', ')[0])
      pickRef.current?.([r.lat, r.lon])
      cityRef.current?.(r)
    } catch (e) {
      console.error('[kedr] Error flying to location:', e)
    }
  }

  const kinds = [...new Set((infra || []).map((f) => f.kind))]

  return (
    <div className={`relative overflow-hidden rounded-2xl border border-border/50 ${className}`}>
      <div className="absolute inset-0"><div ref={el} className="h-full w-full" /></div>
      {failed && <div role="alert" className="absolute inset-0 z-10 grid place-items-center bg-card p-6 text-center">{t?.map_fail}</div>}
      <div className="absolute inset-x-0 top-0 z-10 p-3 sm:right-auto sm:w-full sm:max-w-sm">
        <label className="flex items-center gap-2 rounded-full border border-border/60 bg-card/90 px-4 shadow-soft backdrop-blur">
          <Search className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
          <span className="sr-only">{t?.search_ph}</span>
          <input className="min-h-11 w-full border-0 bg-transparent px-0 text-sm outline-none" placeholder={t?.search_ph} value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <AnimatePresence>
          {res.length > 0 && (
            <motion.ul initial={{ opacity: 0, y: '-0.5rem' }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} className="mt-2 max-h-64 overflow-auto rounded-2xl border border-border/60 bg-card text-sm shadow-soft">
              {res.map((r, k) => <li key={k}><motion.button whileHover={{ x: '0.25rem' }} className="grid min-h-11 w-full cursor-pointer px-4 py-2 text-left" onClick={() => go(r)}><span className="truncate font-semibold">{r.name.split(', ')[0]}</span><span className="truncate text-xs text-muted-foreground">{r.name.split(', ').slice(1).join(', ')}</span></motion.button></li>)}
            </motion.ul>
          )}
        </AnimatePresence>
      </div>
      <button type="button" onClick={() => setBase(base === 'sat' ? 'map' : 'sat')} aria-pressed={base === 'sat'}
        title={base === 'sat' ? t?.map_map : t?.map_sat}
        className="absolute bottom-3 right-3 z-10 flex min-h-11 cursor-pointer items-center gap-2 rounded-full border border-border/60 bg-card/90 px-4 text-xs font-semibold shadow-soft backdrop-blur hover:bg-card">
        <Layers className="h-4 w-4" aria-hidden />{base === 'sat' ? t?.map_map : t?.map_sat}
      </button>
      {kinds.length > 0 && (
        <div className="absolute bottom-16 left-3 z-10 flex max-w-[min(20rem,60%)] flex-wrap gap-x-3 gap-y-1 rounded-2xl bg-card/90 px-3 py-2 text-xs shadow-soft">
          <span className="w-full font-semibold text-muted-foreground">{t?.city_infra}</span>
          {kinds.sort((a, b) => (INFRA_CRITICALITY[b] || 0) - (INFRA_CRITICALITY[a] || 0)).map((k) => (
            <span key={k} className="flex items-center gap-1.5">
              <span className="h-3 w-3 rounded-full border border-white" style={{ background: INFRA_COLOR[k] || INFRA_COLOR.other }} aria-hidden />
              {t?.['infra_' + k] || k}
            </span>
          ))}
        </div>
      )}
      <div className="absolute bottom-3 left-3 z-10 hidden gap-3 rounded-full bg-card/90 px-4 py-1 text-xs shadow-soft lg:flex">
        {Object.entries(COLOR).map(([k, c]) => <span key={k} className="flex items-center gap-2"><StatusDot status={k} />{t?.['s_' + k]}</span>)}
      </div>
      <span ref={cur} className="absolute bottom-16 right-3 z-10 hidden rounded-lg bg-card/90 px-2 py-1 font-mono text-xs lg:block">—</span>
    </div>
  )
}
