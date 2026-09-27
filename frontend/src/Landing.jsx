import { motion } from 'framer-motion'
import { BellRing, CheckCircle2, Clock, Flame, Map as MapIcon, ScanSearch, ShieldCheck } from 'lucide-react'
import { Aurora, Btn, Card, IconTile, Logo, Reveal, StatusDot } from './ui'

const ICONS = [ScanSearch, MapIcon, ShieldCheck, BellRing]
const jump = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth' })

export default function Landing({ t, onTry }) {
  return (
    <>
      <section className="relative isolate overflow-hidden border-b border-dotted border-border px-4 py-20 text-center md:py-32">
        <Aurora />
        <div className="mx-auto max-w-3xl">
          <Reveal>
            <span className="inline-flex items-center gap-2 rounded-full border border-border/60 bg-accent/70 px-3 py-1 font-mono text-micro uppercase text-forest dark:text-white">
              <StatusDot status="CRITICAL_ALERT" live />{t.hero_badge}
            </span>
          </Reveal>
          <h1 className="mt-6 text-display font-extrabold text-primary">
            {t.hero_title.split(' ').map((w, i) => (
              <motion.span key={i} className="inline-block pr-3" initial={{ opacity: 0, y: '0.4em', filter: 'blur(0.4rem)' }} animate={{ opacity: 1, y: 0, filter: 'blur(0rem)' }} transition={{ delay: 0.1 + i * 0.08, duration: 0.5 }}>{w}</motion.span>
            ))}
          </h1>
          <Reveal delay={0.4}><p className="mx-auto mt-6 max-w-xl leading-relaxed">{t.hero_a}<b className="text-destructive">{t.hero_stat}</b>{t.hero_b}</p></Reveal>
          <Reveal delay={0.55}>
            <Btn shimmer className="mt-8 px-8" onClick={onTry}>{t.cta}</Btn>
            <div className="mt-5 flex flex-wrap justify-center gap-x-6 gap-y-2 font-mono text-xs text-muted-foreground">
              <span className="flex items-center gap-2"><CheckCircle2 className="h-4 w-4" aria-hidden />{t.core}</span>
              <span className="flex items-center gap-2"><Clock className="h-4 w-4" aria-hidden />{t.latency}</span>
            </div>
          </Reveal>
        </div>
      </section>

      <section id="features" className="mx-auto max-w-6xl px-4 py-16">
        <Reveal><p className="label-micro">{t.f_label}</p><h2 className="mt-2 text-3xl font-extrabold">{t.f_title}</h2></Reveal>
        <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {t.f.map(([h, p], i) => { const Icon = ICONS[i]; return (
            <Reveal key={h} delay={i * 0.08}>
              <Card className="h-full p-5">
                <IconTile><Icon className="h-5 w-5" aria-hidden /></IconTile>
                <h3 className="mt-4 font-bold">{h}</h3><p className="mt-2 text-sm text-muted-foreground">{p}</p>
              </Card>
            </Reveal>) })}
        </div>
      </section>

      <section id="how" className="bg-brand px-4 py-16 text-white">
        <div className="mx-auto max-w-6xl">
          <Reveal><p className="font-mono text-micro uppercase text-leaf">{t.p_label}</p><h2 className="mt-2 text-3xl font-extrabold">{t.p_title}</h2></Reveal>
          <div className="relative mt-10 grid gap-8 md:grid-cols-4">
            <motion.div aria-hidden className="absolute inset-x-0 top-5 hidden origin-left border-t border-dotted border-leaf/60 md:block" initial={{ scaleX: 0 }} whileInView={{ scaleX: 1 }} viewport={{ once: true }} transition={{ duration: 1.2 }} />
            {t.p.map(([h, p], i) => (
              <Reveal key={h} delay={i * 0.12} className="relative">
                <span className="grid h-11 w-11 place-items-center rounded-full bg-leaf shadow-soft ring-4 ring-leaf/25 font-mono text-sm font-semibold text-forest">{String(i + 1).padStart(2, '0')}</span>
                <h3 className="mt-4 font-bold">{h}</h3><p className="mt-1 text-sm text-leaf/90">{p}</p>
              </Reveal>
            ))}
          </div>
        </div>
      </section>

      <section className="px-4 py-20 text-center">
        <Reveal>
          <h2 className="text-3xl font-extrabold text-primary md:text-4xl">{t.end_title}</h2>
          <p className="mx-auto mt-4 max-w-lg">{t.end_text}</p>
          <Btn shimmer className="mt-8 px-8" onClick={onTry}>{t.cta}</Btn>
        </Reveal>
      </section>

      <footer id="contacts" className="border-t border-border/40 bg-graphite px-4 py-12 text-white/80">
        <div className="mx-auto grid max-w-6xl gap-8 md:grid-cols-3">
          <div>
            <span className="flex items-center gap-2 font-bold text-white"><Logo />КЕДР</span>
            <p className="mt-4 max-w-xs text-sm">{t.foot_text}</p>
          </div>
          <nav aria-label={t.foot_nav}><p className="font-mono text-micro uppercase text-leaf">{t.foot_nav}</p>
            <ul className="mt-3 space-y-1 text-sm">{[['features', t.n1], ['how', t.n2], ['contacts', t.n3]].map(([id, l]) => (
              <li key={id}><motion.button whileHover={{ x: '0.25rem' }} className="min-h-11 cursor-pointer text-left" onClick={() => jump(id)}>{l}</motion.button></li>))}</ul></nav>
          <div className="md:text-right"><p className="font-mono text-micro uppercase text-leaf">{t.foot_ver}</p>
            <span className="mt-3 inline-flex items-center gap-2 rounded-xl bg-black/40 px-3 py-2 font-mono text-xs"><StatusDot status="PENDING_VERIFICATION" /> v1.2 · YOLOv8</span></div>
        </div>
        <p className="mx-auto mt-10 max-w-6xl text-xs text-white/60">© 2026 КЕДР. {t.rights}</p>
      </footer>
    </>
  )
}
