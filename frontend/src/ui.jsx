import { useEffect, useState } from 'react'
import { AnimatePresence, motion, useMotionTemplate, useMotionValue } from 'framer-motion'
import { Flame, X } from 'lucide-react'

export const spring = { type: 'spring', stiffness: 380, damping: 30 }
const TONE = { primary: 'bg-primary text-primary-foreground', warn: 'bg-destructive text-white', ghost: 'border border-border/60 bg-card text-foreground' }

export function useMedia(q) {
  const [m, setM] = useState(() => matchMedia(q).matches)
  useEffect(() => { const l = matchMedia(q), f = () => setM(l.matches); l.addEventListener('change', f); return () => l.removeEventListener('change', f) }, [q])
  return m
}

export function Btn({ v = 'primary', shimmer, className = '', children, ...p }) {
  const Tag = p.href ? motion.a : motion.button
  return (
    <Tag whileHover={{ scale: 1.03 }} whileTap={{ scale: 0.96 }} transition={spring}
      className={`relative inline-flex min-h-11 cursor-pointer items-center justify-center gap-2 overflow-hidden rounded-full px-5 text-sm font-semibold disabled:cursor-not-allowed disabled:opacity-50 ${TONE[v]} ${className}`} {...p}>
      {shimmer && <motion.span aria-hidden className="absolute inset-y-0 w-1/3 -skew-x-12 bg-white/25" initial={{ x: '-150%' }} animate={{ x: '450%' }} transition={{ duration: 1.8, repeat: Infinity, repeatDelay: 2.5, ease: 'easeInOut' }} />}
      <span className="relative inline-flex items-center gap-2">{children}</span>
    </Tag>
  )
}

export const Badge = ({ status, children }) => (
  <span className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-gradient-to-b from-card to-muted px-3 py-1 text-xs font-semibold shadow-soft">
    <StatusDot status={status} live={status === 'CRITICAL_ALERT'} />{children}
  </span>
)

export const Reveal = ({ children, delay = 0, className }) => (
  <motion.div className={className} initial={{ opacity: 0, y: '1.5rem' }} whileInView={{ opacity: 1, y: 0 }} viewport={{ once: true, margin: '-10%' }} transition={{ duration: 0.5, delay, ease: 'easeOut' }}>{children}</motion.div>
)

export const Skeleton = ({ className = 'h-24' }) => (
  <motion.div aria-hidden className={`rounded-2xl bg-muted ${className}`} animate={{ opacity: [0.4, 1, 0.4] }} transition={{ duration: 1.4, repeat: Infinity }} />
)

export function Card({ children, className = '', lift = true, ...p }) {
  const x = useMotionValue(50), y = useMotionValue(50)
  const bg = useMotionTemplate`radial-gradient(circle at ${x}% ${y}%, rgb(var(--accent) / 0.6), transparent 65%)`
  const move = (e) => { const r = e.currentTarget.getBoundingClientRect(); x.set(((e.clientX - r.left) / r.width) * 100); y.set(((e.clientY - r.top) / r.height) * 100) }
  return (
    <motion.div onPointerMove={move} initial="rest" whileHover="hover" variants={{ rest: { y: 0 }, hover: { y: lift ? '-0.25rem' : 0 } }} transition={spring}
      className={`relative overflow-hidden rounded-2xl border border-border/50 bg-card ${className}`} {...p}>
      <motion.div aria-hidden className="pointer-events-none absolute inset-0" variants={{ rest: { opacity: 0 }, hover: { opacity: 1 } }} style={{ background: bg }} />
      <div className="relative">{children}</div>
    </motion.div>
  )
}

export function Tabs({ tabs, value, onChange, id }) {
  return (
    <div role="tablist" className="flex gap-1 rounded-full bg-muted p-1">
      {tabs.map(([k, label]) => (
        <button key={k} role="tab" aria-selected={value === k} onClick={() => onChange(k)} className="relative min-h-11 flex-1 cursor-pointer rounded-full px-4 text-sm font-semibold">
          {value === k && <motion.span layoutId={id} transition={spring} className="absolute inset-0 rounded-full bg-card shadow-soft" />}
          <span className="relative">{label}</span>
        </button>
      ))}
    </div>
  )
}

export function Modal({ open, onClose, title, closeLabel, children }) {
  useEffect(() => { if (!open) return; const k = (e) => e.key === 'Escape' && onClose(); addEventListener('keydown', k); return () => removeEventListener('keydown', k) }, [open, onClose])
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="fixed inset-0 z-50 grid place-items-end bg-foreground/40 p-4 backdrop-blur-sm sm:place-items-center" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose}>
          <motion.div role="dialog" aria-modal="true" aria-label={title} onClick={(e) => e.stopPropagation()} initial={{ y: '2rem', opacity: 0, scale: 0.98 }} animate={{ y: 0, opacity: 1, scale: 1 }} exit={{ y: '2rem', opacity: 0 }} transition={spring}
            className="w-full max-w-md rounded-3xl border border-border/50 bg-card p-6 shadow-soft">
            <div className="mb-4 flex items-center justify-between gap-4">
              <h2 className="text-lg font-extrabold">{title}</h2>
              <motion.button aria-label={closeLabel} whileTap={{ scale: 0.9 }} onClick={onClose} className="grid h-11 w-11 cursor-pointer place-items-center rounded-full bg-muted"><X className="h-5 w-5" /></motion.button>
            </div>
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

const HALO = { CRITICAL_ALERT: 'bg-tomato', CONFIRMED: 'bg-carrot', PENDING_VERIFICATION: 'bg-sunshine' }
export function StatusDot({ status, live, large }) {
  const tone = HALO[status] || 'bg-moss'
  return (
    <span aria-hidden className={`relative grid shrink-0 place-items-center ${large ? 'h-3.5 w-3.5' : 'h-2.5 w-2.5'}`}>
      {live && <motion.span className={`absolute inset-0 rounded-full ${tone}`} animate={{ scale: [1, 2.6], opacity: [0.55, 0] }} transition={{ duration: 1.6, repeat: Infinity, ease: 'easeOut' }} />}
      <span className={`relative h-full w-full rounded-full shadow-soft ring-2 ring-card ${tone}`} />
    </span>
  )
}

export const IconTile = ({ children, className = '' }) => (
  <motion.span whileHover={{ rotate: -6, scale: 1.08 }} className={`grid h-11 w-11 place-items-center rounded-xl bg-gradient-to-br from-accent to-muted text-primary shadow-soft ring-1 ring-inset ring-border/50 ${className}`}>{children}</motion.span>
)

export const Logo = () => (
  <motion.span whileHover={{ rotate: [0, -8, 8, 0] }} className="grid h-9 w-9 place-items-center rounded-xl bg-gradient-to-br from-leaf to-moss text-forest shadow-soft ring-1 ring-inset ring-white/50"><Flame className="h-5 w-5" aria-hidden /></motion.span>
)

export const Aurora = () => (
  <div aria-hidden className="pointer-events-none absolute inset-0 -z-10 overflow-hidden">
    <div className="absolute inset-0 bg-dot-grid bg-grid opacity-70" />
    {[['-top-16 left-1/4', 14], ['top-1/3 -right-16', 18], ['-bottom-16 left-0', 22]].map(([pos, d], i) => (
      <motion.div key={i} className={`absolute h-64 w-64 rounded-full bg-orb blur-3xl md:h-96 md:w-96 ${pos}`} animate={{ x: ['0%', '15%', '0%'], y: ['0%', '-12%', '0%'] }} transition={{ duration: d, repeat: Infinity, ease: 'easeInOut' }} />
    ))}
  </div>
)
