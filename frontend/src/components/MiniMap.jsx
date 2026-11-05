import { useEffect, useRef, useState } from 'react'
import maplibregl from 'maplibre-gl'
import 'maplibre-gl/dist/maplibre-gl.css'

// Satellite imagery is the useful backdrop for picking a fire location.
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

/**
 * Small click-to-place map used to set a fire location.
 *
 * Clicking the map is the primary way to choose the point; the coordinates are
 * echoed back to the caller so they can be shown (and pasted) as well.
 */
export default function MiniMap({ value, onChange, className = 'h-56', zoom = 12, t }) {
  const holder = useRef(null)
  const map = useRef(null)
  const marker = useRef(null)
  const changeRef = useRef(onChange)
  const [ready, setReady] = useState(false)
  changeRef.current = onChange

  useEffect(() => {
    if (!holder.current) return
    const start = value && Number.isFinite(value[0]) ? value : [92.9, 56.0]
    let m
    try {
      m = new maplibregl.Map({
        container: holder.current,
        style: SAT,
        center: start,
        zoom: value ? zoom : 3,
        attributionControl: { compact: true },
      })
    } catch (e) {
      console.error('[kedr minmap]', e)
      return
    }
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right')
    m.on('click', (e) => {
      const p = e.lngLat.wrap()
      changeRef.current?.([p.lat, p.lng])
    })
    m.on('load', () => {
      setReady(true)
    })
    map.current = m
    return () => { m.remove(); map.current = null; marker.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The marker appears only once a point exists: an unexplained pin on an empty
  // map is confusing. It follows the point set from the map, search or EXIF.
  useEffect(() => {
    const m = map.current
    if (!ready || !m || !value || !Number.isFinite(value[0])) return
    if (!marker.current) {
      marker.current = new maplibregl.Marker({ color: '#F96015', draggable: true })
        .setLngLat(value).addTo(m)
      marker.current.on('dragend', () => {
        const p = marker.current.getLngLat()
        changeRef.current?.([p.lat, p.lng])
      })
      m.flyTo({ center: value, zoom })
      return
    }
    const cur = marker.current.getLngLat()
    if (Math.abs(cur.lat - value[0]) < 1e-6 && Math.abs(cur.lng - value[1]) < 1e-6) return
    marker.current.setLngLat(value)
    m.flyTo({ center: value, zoom })
  }, [value, ready, zoom])

  return (
    <div className={`relative overflow-hidden rounded-xl border border-border/50 ${className}`}>
      <div ref={holder} className="h-full w-full" />
      {t?.pick_on_map && (
        <span className="pointer-events-none absolute left-2 top-2 rounded-full bg-card/90 px-3 py-1 text-xs font-semibold shadow-soft">
          {t.pick_on_map}
        </span>
      )}
    </div>
  )
}
