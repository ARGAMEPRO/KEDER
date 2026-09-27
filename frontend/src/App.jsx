import { Suspense, lazy, useEffect, useRef, useState } from 'react'
import { AnimatePresence, MotionConfig, motion } from 'framer-motion'
import { Flame, Home, LogOut, Map as MapIcon, Moon, ScanSearch, Sun, User } from 'lucide-react'
import { api, tok, wsUrl } from './api'
import { dict } from './i18n'
import { extra } from './strings'
import Landing from './Landing'
import ErrorBoundary from './ErrorBoundary'
import { Report } from './parts'
import { Btn, Card, Logo, Modal, Reveal, Skeleton, Tabs, spring } from './ui'

const Workspace = lazy(() => import('./Workspace')) // map + MapLibre load only when the tab is opened
const VIEWS = ['home', 'analyze', 'map']

function beep() {
  try {
    const c = new AudioContext(), o = c.createOscillator()
    o.type = 'square'; o.frequency.value = 880; o.connect(c.destination); o.start()
    setTimeout(() => { o.stop(); c.close() }, 700)
  } catch { /* audio blocked */ }
}

function AuthForm({ t, onUser }) {
  const [mode, setMode] = useState('login'), [f, setF] = useState({ email: '', password: '', full_name: '' }), [err, setErr] = useState(''), [busy, setBusy] = useState(false)
  const go = async (e) => {
    e.preventDefault(); setBusy(true); setErr('')
    try {
      const path = mode === 'login' ? '/auth/login' : '/auth/register'
      const body = mode === 'login' ? { email: f.email, password: f.password } : f
      const r = await api(path, { json: body })
      tok.set(r.access_token); onUser(r.user)
    } catch (x) { setErr(x.message) }
    setBusy(false)
  }
  return (
    <form onSubmit={go} className="space-y-3">
      <Tabs id="auth-tabs" value={mode} onChange={setMode} tabs={[['login', t.login], ['register', t.register]]} />
      {mode === 'register' && (
        <label className="block space-y-1"><span className="text-sm font-semibold">{t.full_name}</span>
          <input className="field" autoComplete="name" value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></label>
      )}
      <label className="block space-y-1"><span className="text-sm font-semibold">{t.email}</span>
        <input className="field" type="email" autoComplete="username" required value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></label>
      <label className="block space-y-1"><span className="text-sm font-semibold">{t.password}</span>
        <input className="field" type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'} required minLength={8} value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></label>
      {mode === 'register' && <p className="text-xs text-muted-foreground">{t.password_rules}</p>}
      {err && <p role="alert" className="text-destructive">{err}</p>}
      <Btn type="submit" className="w-full" disabled={busy}>{busy ? t.wait : mode === 'login' ? t.login : t.register_submit}</Btn>
      {mode === 'login' && (
        <div className="flex flex-wrap gap-2">
          {['forester', 'mchs'].map((r) => <Btn key={r} type="button" v="ghost" onClick={() => setF({ ...f, email: `${r}@kedr.ru`, password: '123456' })}>{t.demo}: {t['role_' + r]}</Btn>)}
        </div>
      )}
    </form>
  )
}

export default function App() {
  const [lang, setLang] = useState(localStorage.kedr_lang || 'ru')
  const [dark, setDark] = useState(localStorage.kedr_theme ? localStorage.kedr_theme === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches)
  const [user, setUser] = useState(null), [login, setLogin] = useState(false), [tick, setTick] = useState(0), [alarm, setAlarm] = useState(null)
  const [health, setHealth] = useState(null)
  const raw = location.hash.slice(2), [view, setView] = useState(raw === 'dash' ? 'map' : VIEWS.includes(raw) ? raw : 'home')
  const main = useRef(), t = { ...dict[lang], ...extra[lang] }, role = user?.role
  const go = (v) => { location.hash = '/' + v; setView(v) }
  useEffect(() => { const h = () => { const r = location.hash.slice(2); setView(r === 'dash' ? 'map' : VIEWS.includes(r) ? r : 'home') }; addEventListener('hashchange', h); return () => removeEventListener('hashchange', h) }, [])
  useEffect(() => { main.current?.focus({ preventScroll: true }); scrollTo(0, 0) }, [view])
  useEffect(() => { api('/health').then(setHealth).catch(() => {}) }, [])
  useEffect(() => { document.documentElement.classList.toggle('dark', dark); localStorage.kedr_theme = dark ? 'dark' : 'light' }, [dark])
  useEffect(() => { localStorage.kedr_lang = lang; document.documentElement.lang = lang }, [lang])
  useEffect(() => { if (tok.get()) api('/auth/me').then(setUser).catch(() => tok.set(null)) }, [])
  useEffect(() => {
    if (role !== 'forester' && role !== 'mchs') return
    let ws, stop = false
    const connect = () => {
      ws = new WebSocket(wsUrl())
      ws.onmessage = (e) => { const m = JSON.parse(e.data); setTick((x) => x + 1); if (m.type === 'CRITICAL_ALERT' && role === 'mchs') { setAlarm(m.incident); beep() } if (m.type === 'INCIDENT_DELETED') setTick((x) => x + 1) }
      ws.onclose = () => { if (!stop) setTimeout(connect, 3000) }
    }
    connect()
    return () => { stop = true; ws?.close() }
  }, [role])
  const nav = [['home', Home, t.nav_home], ['analyze', ScanSearch, t.nav_analyze], ['map', MapIcon, t.nav_map]]
  const logout = () => { tok.set(null); setUser(null) }
  const ctl = 'grid h-11 min-w-11 cursor-pointer place-items-center rounded-full'
  return (
    <MotionConfig reducedMotion="user" transition={spring}>
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-2 focus:rounded-full focus:bg-card focus:p-3">{t.skip}</a>
      <header className="sticky top-0 z-40 flex min-h-nav items-center justify-between gap-4 bg-brand px-4 text-white shadow-soft md:grid md:grid-cols-header">
        <motion.button whileTap={{ scale: 0.96 }} onClick={() => go('home')} className="flex min-h-11 cursor-pointer items-center gap-2 justify-self-start font-extrabold">
          <Logo />КЕДР
        </motion.button>
        <nav aria-label="main" className="hidden gap-1 justify-self-center rounded-full border border-white/15 bg-white/10 p-1 shadow-soft backdrop-blur md:flex">
          {nav.map(([k, Icon, label]) => (
            <button key={k} aria-current={view === k ? 'page' : undefined} onClick={() => go(k)} className="relative flex min-h-11 cursor-pointer items-center gap-2 rounded-full px-4 text-sm font-semibold">
              {view === k && <motion.span layoutId="nav-pill" className="absolute inset-0 rounded-full bg-white shadow-soft" />}
              <span className={`relative flex items-center gap-2 ${view === k ? 'text-forest' : ''}`}><Icon className="h-4 w-4" aria-hidden />{label}</span>
            </button>
          ))}
        </nav>
        <div className="flex items-center gap-1 justify-self-end font-mono text-xs">
          {user && <span className="hidden px-2 xl:inline">{user.email}</span>}
          <motion.button whileTap={{ scale: 0.92 }} className={ctl + ' px-2'} aria-label="language" onClick={() => setLang(lang === 'ru' ? 'en' : 'ru')}>
            <span><b className={lang === 'ru' ? '' : 'opacity-60'}>RU</b> / <b className={lang === 'en' ? '' : 'opacity-60'}>EN</b></span>
          </motion.button>
          <motion.button whileTap={{ scale: 0.92 }} className={ctl} aria-label="theme" onClick={() => setDark(!dark)}>
            <motion.span key={String(dark)} initial={{ rotate: -90, opacity: 0 }} animate={{ rotate: 0, opacity: 1 }}>{dark ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}</motion.span>
          </motion.button>
          <motion.button whileTap={{ scale: 0.92 }} className={ctl} aria-label={user ? t.logout : t.login} onClick={user ? logout : () => setLogin(true)}>
            {user ? <LogOut className="h-5 w-5" /> : <User className="h-5 w-5" />}
          </motion.button>
        </div>
      </header>

      <AnimatePresence>
        {alarm && (
          <motion.div role="alert" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden bg-destructive text-white">
            <div className="flex flex-wrap items-center justify-between gap-2 p-3 font-semibold">
              <span>{t.alarm}: #{alarm.id} <span className="font-mono">{alarm.lat?.toFixed(6)}, {alarm.lon?.toFixed(6)}</span></span>
              <Btn v="ghost" className="text-foreground" onClick={() => setAlarm(null)}>{t.dismiss}</Btn>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <main id="main" ref={main} tabIndex={-1} className="pb-nav outline-none md:pb-0">
        <AnimatePresence mode="wait">
          <motion.div key={view} initial={{ opacity: 0, y: '0.75rem' }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}>
          <ErrorBoundary key={view} title={t.err_title} retry={t.retry}>
            {view === 'home' && <Landing t={t} onTry={() => go('analyze')} />}
            {view === 'analyze' && (
              <div className="mx-auto grid max-w-5xl gap-8 px-4 py-10 md:grid-cols-5">
                <Reveal className="space-y-3 md:col-span-2">
                  <h1 className="text-3xl font-extrabold text-primary">{t.analyze_h}</h1><p>{t.analyze_p}</p>
                  <Card lift={false} className="p-4 text-sm">{t.tiers}</Card>
                  {health && !health.model && <Card lift={false} role="status" className="border-destructive/60 p-4 text-sm">{t.model_off}</Card>}
                </Reveal>
                <div className="md:col-span-3"><Report t={t} canPdf={role === 'mchs'} /></div>
              </div>
            )}
            {view === 'map' && <Suspense fallback={<Skeleton className="m-4 h-map" />}><Workspace t={t} role={role} dark={dark} lang={lang} tick={tick} go={go} /></Suspense>}
          </ErrorBoundary>
          </motion.div>
        </AnimatePresence>
      </main>

      <nav aria-label="main" className="pb-safe fixed inset-x-0 bottom-0 z-40 grid grid-cols-3 border-t border-border/50 bg-card/95 backdrop-blur md:hidden">
        {nav.map(([k, Icon, label]) => (
          <motion.button key={k} whileTap={{ scale: 0.92 }} aria-current={view === k ? 'page' : undefined} onClick={() => go(k)} className={`relative grid min-h-nav cursor-pointer place-items-center content-center gap-1 text-xs font-semibold ${view === k ? 'text-primary' : 'text-muted-foreground'}`}>
            {view === k && <motion.span layoutId="nav-dot" className="absolute inset-x-1/4 top-0 h-1 rounded-full bg-primary" />}
            <Icon className="h-5 w-5" aria-hidden />{label}
          </motion.button>
        ))}
      </nav>

      <Modal open={login && !user} onClose={() => setLogin(false)} title={t.login} closeLabel={t.close}><AuthForm t={t} onUser={(u) => { setUser(u); setLogin(false) }} /></Modal>
    </MotionConfig>
  )
}
