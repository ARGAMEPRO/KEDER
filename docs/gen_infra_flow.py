"""Генератор схемы docs/infra-request-flow.svg.

Один запрос «покажи инфраструктуру вокруг точки» и все пути, которые он
проходит: браузер -> nginx -> FastAPI -> цепочка провайдеров -> кэши -> ответ.
Запуск: python3 docs/gen_infra_flow.py
"""
from pathlib import Path
from xml.sax.saxutils import escape

W, H = 1860, 1080
OUT = Path(__file__).with_name("infra-request-flow.svg")

COL = {
    "client": "#1f6f8b",
    "edge": "#2b7a78",
    "api": "#3c6e71",
    "core": "#8a5a44",
    "osm": "#5b8c5a",
    "geo": "#b07d2b",
    "gis": "#7a5195",
    "cache": "#4a5568",
    "aux": "#9c4221",
}

N = {}


def node(key, x, y, w, h, title, sub="", color="core", small=False):
    N[key] = dict(x=x, y=y, w=w, h=h, title=title, sub=sub, color=color, small=small)


# --- клиент и вход -----------------------------------------------------------
node("browser", 30, 600, 200, 72, "Браузер", "панель города, MapLibre", "client")
node("fetch", 280, 600, 190, 72, "React api.js", "fetch с таймаутом", "client")
node("nginx", 280, 730, 190, 72, "nginx", "/api → backend:8000", "edge")
node("fastapi", 530, 600, 210, 72, "FastAPI", "GET /geo/infrastructure", "api")
node("ctx", 530, 730, 210, 82, "geo_context()", "asyncio.gather ×4", "api")

# --- ядро --------------------------------------------------------------------
node("infra", 800, 470, 220, 84, "infrastructure_near()", "OSM + резерв, радиус 15 км", "core")
node("final", 800, 600, 220, 84, "post-process", "дистанция, фильтр, сорт, cap 120", "core")
node("resp", 280, 470, 190, 72, "Ответ JSON", "source + facilities + counts", "client")

# --- overpass ----------------------------------------------------------------
node("osm", 1050, 180, 210, 70, "Overpass API", "полный OSM, 800 объектов", "osm")
node("m_de", 1330, 60, 210, 44, "overpass-api.de", "502 at capacity", "osm", True)
node("m_zde", 1330, 116, 210, 44, "z.overpass-api.de", "502 at capacity", "osm", True)
node("m_mail", 1330, 172, 210, 44, "maps.mail.ru", "200 / 504, 25–60 с", "osm", True)
node("m_relay", 1330, 228, 210, 44, "Cloudflare Worker", "перебирает 3 зеркала", "osm", True)
node("m_kumi", 1330, 284, 210, 44, "overpass.kumi.systems", "таймаут", "osm", True)
node("m_jp", 1330, 340, 210, 44, "overpass.osm.jp", "нет соединения", "osm", True)
node("m_coffee", 1330, 396, 210, 44, "overpass.private.coffee", "таймаут", "osm", True)
node("relay_up1", 1600, 200, 180, 36, "relay → .de", "", "osm", True)
node("relay_up2", 1600, 244, 180, 36, "relay → mail.ru", "", "osm", True)
node("relay_up3", 1600, 288, 180, 36, "relay → kumi", "", "osm", True)

# --- geoapify ----------------------------------------------------------------
node("geo", 1050, 600, 210, 70, "Geoapify Places", "OSM-выборка, free tier", "geo")
node("g_med", 1330, 560, 210, 42, "больницы/полиция", "200, 15 км", "geo", True)
node("g_power", 1330, 610, 210, 42, "энергетика", "подстанции, 15 км", "geo", True)
node("g_prod", 1330, 660, 210, 42, "промышленность", "НПЗ/химзавод, 6 км", "geo", True)
node("g_air", 1330, 710, 210, 42, "аэропорты", "отдельный запрос", "geo", True)
node("g_mass", 1330, 760, 210, 42, "школы/садики/аптеки", "200, 15 км", "geo", True)

# --- 2gis --------------------------------------------------------------------
node("gis", 1050, 880, 210, 62, "2GIS Catalog", "ключ TWOGIS_KEY (опция)", "gis")

# --- параллельные ветки geo_context -----------------------------------------
node("weather", 800, 760, 220, 66, "weather()", "met.no → open-meteo → OWM → wttr", "aux")
node("reverse", 800, 850, 220, 66, "reverse_geocode()", "Nominatim → локальная база", "aux")
node("nearby", 800, 940, 220, 66, "nearby()", "Overpass → локальная база", "aux")

# --- кэши --------------------------------------------------------------------
node("c_mem", 1600, 480, 210, 58, "_CACHE (RAM)", "INFRA_TTL 30 мин", "cache")
node("c_sql", 1600, 548, 210, 58, "pcache SQLite", "кэш 30 дней, том docker", "cache")
node("c_ovp", 1600, 616, 210, 58, "кэш Overpass", "по ключу точки, 30 мин", "cache")
node("c_neg", 1600, 684, 210, 58, "negative cache", "60 с после падения", "cache")


def edge(a, b, label="", dashed=False, color=None, both=False, bend=0.0):
    A, B = N[a], N[b]
    x1, y1 = A["x"] + A["w"], A["y"] + A["h"] / 2
    x2, y2 = B["x"], B["y"] + B["h"] / 2
    if x2 < x1:  # обратное направление
        x1, y1 = A["x"], A["y"] + A["h"] / 2
        x2, y2 = B["x"] + B["w"], B["y"] + B["h"] / 2
    dx = max(40, abs(x2 - x1) / 2)
    c1 = (x1 + dx, y1 + bend)
    c2 = (x2 - dx, y2 + bend)
    stroke = color or COL.get(A["color"], "#555")
    d = f"M{x1:.1f},{y1:.1f} C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {x2:.1f},{y2:.1f}"
    out = [f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="1.6"'
           f'{" stroke-dasharray=\"6 4\"" if dashed else ""} marker-end="url(#arw)"/>']
    if label:
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2 - 6
        anchor = "middle"
        out.append(f'<text x="{mx:.1f}" y="{my:.1f}" class="lbl" text-anchor="{anchor}">{escape(label)}</text>')
    return "".join(out)


def box(k):
    n = N[k]
    c = COL[n["color"]]
    fs = 12.5 if n["small"] else 15
    compact = n["h"] < 52
    ty = n["y"] + (11 if compact else 24) if n["sub"] else n["y"] + n["h"] / 2 + 5
    parts = [f'<g class="node"><rect x="{n["x"]}" y="{n["y"]}" width="{n["w"]}" height="{n["h"]}" rx="9" '
             f'fill="#fff" stroke="{c}" stroke-width="1.8"/>',
             f'<rect x="{n["x"]}" y="{n["y"]}" width="5" height="{n["h"]}" rx="2.5" fill="{c}"/>',
             f'<text x="{n["x"] + 14}" y="{ty}" class="t" '
             f'fill="{c}" font-size="{fs}">{escape(n["title"])}</text>']
    if n["sub"]:
        sy = n["y"] + 29 if compact else n["y"] + 42
        parts.append(f'<text x="{n["x"] + 14}" y="{sy}" class="s">{escape(n["sub"])}</text>')
    parts.append("</g>")
    return "".join(parts)


edges = [
    edge("browser", "fetch", "клик по городу"),
    edge("fetch", "nginx", "/api/v1"),
    edge("nginx", "fastapi", "proxy_pass"),
    edge("fastapi", "ctx"),
    edge("ctx", "infra", "gather"),
    edge("ctx", "weather", "gather"),
    edge("ctx", "reverse", "gather"),
    edge("ctx", "nearby", "gather"),
    edge("infra", "osm", "задача 1: 1.5 с форы"),
    edge("osm", "m_de"),
    edge("osm", "m_zde"),
    edge("osm", "m_mail"),
    edge("osm", "m_relay"),
    edge("osm", "m_kumi"),
    edge("osm", "m_jp"),
    edge("osm", "m_coffee"),
    edge("m_relay", "relay_up1", dashed=True),
    edge("m_relay", "relay_up2", dashed=True),
    edge("m_relay", "relay_up3", dashed=True),
    edge("infra", "geo", "параллельно"),
    edge("geo", "g_med"),
    edge("geo", "g_power"),
    edge("geo", "g_prod"),
    edge("geo", "g_air"),
    edge("geo", "g_mass"),
    edge("infra", "gis", "TWOGIS_KEY"),
    edge("infra", "final", "строки"),
    edge("osm", "c_ovp", "кэш ответа", dashed=True),
    edge("osm", "c_neg", "падение", dashed=True),
    edge("final", "c_mem", "первый непустой", dashed=True),
    edge("final", "c_sql", "если все упали", dashed=True),
    edge("final", "resp", "120 объектов"),
    edge("resp", "browser", "JSON"),
]

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" font-family="Inter, 'Segoe UI', Arial, sans-serif">
<defs>
  <marker id="arw" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0,0 L10,5 L0,10 z" fill="#6b7280"/>
  </marker>
  <style>
    .t {{ font-weight: 600; }}
    .s {{ font-size: 11.5px; fill: #6b7280; }}
    .lbl {{ font-size: 11px; fill: #4b5563; }}
    .h {{ font-size: 22px; font-weight: 700; fill: #111827; }}
    .hs {{ font-size: 13px; fill: #6b7280; }}
    .col {{ font-size: 12px; font-weight: 700; fill: #9ca3af; letter-spacing: .08em; }}
  </style>
</defs>
<rect width="{W}" height="{H}" fill="#fbfbfd"/>
<text x="40" y="46" class="h">Путь запроса: инфраструктура вокруг точки</text>
<text x="40" y="70" class="hs">GET /api/v1/geo/infrastructure → geo_context → infrastructure_near → Overpass / Geoapify / 2GIS / кэш</text>

<text x="40" y="580" class="col">КЛИЕНТ</text>
<text x="280" y="580" class="col">ВХОД</text>
<text x="530" y="580" class="col">БЭКЕНД</text>
<text x="800" y="440" class="col">ЦЕПОЧКА</text>
<text x="1050" y="150" class="col">ПРОВАЙДЕРЫ</text>
<text x="1600" y="450" class="col">КЭШИ</text>

{"".join(edges)}
{"".join(box(k) for k in N)}
</svg>'''

OUT.write_text(svg, encoding="utf-8")
print(f"{OUT} — {len(svg)/1024:.0f} KB, nodes {len(N)}, edges {len(edges)}")


# --- PNG-версия (та же геометрия, рисуется Pillow) ---------------------------
FONT = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"


def render_png(scale: float = 1.6, path: Path | None = None) -> Path:
    from PIL import Image, ImageDraw, ImageFont

    out = path or OUT.with_suffix(".png")
    S = scale
    img = Image.new("RGB", (int(W * S), int(H * S)), "#fbfbfd")
    d = ImageDraw.Draw(img)

    def f(sz):
        return ImageFont.truetype(FONT, int(sz * S))

    f_title, f_sub, f_col = f(22), f(13), f(12)
    f_t, f_s, f_lbl = f(14), f(11), f(10.5)

    d.text((40 * S, 30 * S), "Путь запроса: инфраструктура вокруг точки", font=f_title, fill="#111827")
    d.text((40 * S, 60 * S), "GET /api/v1/geo/infrastructure → geo_context → infrastructure_near → Overpass / Geoapify / 2GIS / кэш",
           font=f_sub, fill="#6b7280")
    for x, y, label in [(40, 580, "КЛИЕНТ"), (280, 580, "ВХОД"), (530, 580, "БЭКЕНД"),
                        (800, 440, "ЦЕПОЧКА"), (1050, 150, "ПРОВАЙДЕРЫ"), (1600, 450, "КЭШИ")]:
        d.text((x * S, y * S), label, font=f_col, fill="#9ca3af")

    # рёбра
    for a, b, label, dashed in EDGE_LIST:
        A, B = N[a], N[b]
        x1, y1 = A["x"] + A["w"], A["y"] + A["h"] / 2
        x2, y2 = B["x"], B["y"] + B["h"] / 2
        if x2 < x1:
            x1, y1 = A["x"], A["y"] + A["h"] / 2
            x2, y2 = B["x"] + B["w"], B["y"] + B["h"] / 2
        dx = max(40, abs(x2 - x1) / 2)
        c1, c2 = (x1 + dx, y1), (x2 - dx, y2)
        stroke = COL.get(A["color"], "#555")
        pts = []
        for i in range(25):
            t = i / 24
            mt = 1 - t
            px = mt ** 3 * x1 + 3 * mt * mt * t * c1[0] + 3 * mt * t * t * c2[0] + t ** 3 * x2
            py = mt ** 3 * y1 + 3 * mt * mt * t * c1[1] + 3 * mt * t * t * c2[1] + t ** 3 * y2
            pts.append((px * S, py * S))
        if dashed:
            for i in range(0, len(pts) - 1, 4):
                d.line(pts[i:i + 2] if len(pts[i:i + 2]) > 1 else [pts[i], pts[i]], fill=stroke, width=int(1.5 * S))
        else:
            d.line(pts, fill=stroke, width=int(1.5 * S), joint="curve")
        # стрелка
        ax, ay = pts[-1]
        bx, by = pts[-3]
        import math as _m
        ang = _m.atan2(ay - by, ax - bx)
        size = 7 * S
        d.polygon([(ax, ay),
                   (ax - size * _m.cos(ang - 0.4), ay - size * _m.sin(ang - 0.4)),
                   (ax - size * _m.cos(ang + 0.4), ay - size * _m.sin(ang + 0.4))], fill="#6b7280")
        if label:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2 - 6
            tw = d.textlength(label, font=f_lbl)
            d.text((mx * S - tw / 2, my * S), label, font=f_lbl, fill="#4b5563")

    # узлы
    for k, n in N.items():
        x, y, w, h = n["x"] * S, n["y"] * S, n["w"] * S, n["h"] * S
        c = COL[n["color"]]
        d.rounded_rectangle([x, y, x + w, y + h], radius=9 * S, fill="white", outline=c, width=int(1.8 * S))
        d.rounded_rectangle([x, y, x + 5 * S, y + h], radius=2.5 * S, fill=c)
        compact = n["h"] < 52
        ty = y + ((11 if compact else 24) * S if n["sub"] else h / 2 - 8 * S)
        d.text((x + 14 * S, ty), n["title"], font=f_t, fill=c)
        if n["sub"]:
            d.text((x + 14 * S, y + ((29 if compact else 42) * S)), n["sub"], font=f_s, fill="#6b7280")

    img.save(out, "PNG", optimize=True)
    return out


EDGE_LIST = [
    ("browser", "fetch", "клик по городу", False),
    ("fetch", "nginx", "/api/v1", False),
    ("nginx", "fastapi", "proxy_pass", False),
    ("fastapi", "ctx", "", False),
    ("ctx", "infra", "gather", False),
    ("ctx", "weather", "gather", False),
    ("ctx", "reverse", "gather", False),
    ("ctx", "nearby", "gather", False),
    ("infra", "osm", "задача 1: 1.5 с форы", False),
    ("osm", "m_de", "", False),
    ("osm", "m_zde", "", False),
    ("osm", "m_mail", "", False),
    ("osm", "m_relay", "", False),
    ("osm", "m_kumi", "", False),
    ("osm", "m_jp", "", False),
    ("osm", "m_coffee", "", False),
    ("m_relay", "relay_up1", "", True),
    ("m_relay", "relay_up2", "", True),
    ("m_relay", "relay_up3", "", True),
    ("infra", "geo", "параллельно", False),
    ("geo", "g_med", "", False),
    ("geo", "g_power", "", False),
    ("geo", "g_prod", "", False),
    ("geo", "g_air", "", False),
    ("geo", "g_mass", "", False),
    ("infra", "gis", "TWOGIS_KEY", False),
    ("infra", "final", "строки", False),
    ("osm", "c_ovp", "кэш ответа", True),
    ("osm", "c_neg", "падение", True),
    ("final", "c_mem", "первый непустой", True),
    ("final", "c_sql", "если все упали", True),
    ("final", "resp", "120 объектов", False),
    ("resp", "browser", "JSON", False),
]

if __name__ == "__main__":
    png = render_png()
    print(f"{png} — {png.stat().st_size / 1024:.0f} KB")
