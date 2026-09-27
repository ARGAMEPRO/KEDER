export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Семантические токены (значения — в index.css, отдельно для светлой и тёмной темы)
        background: "rgb(var(--background) / <alpha-value>)",
        foreground: "rgb(var(--foreground) / <alpha-value>)",
        card: "rgb(var(--card) / <alpha-value>)",
        muted: {
          DEFAULT: "rgb(var(--muted) / <alpha-value>)",
          foreground: "rgb(var(--muted-fg) / <alpha-value>)",
        },
        border: "rgb(var(--border) / <alpha-value>)",
        primary: {
          DEFAULT: "rgb(var(--primary) / <alpha-value>)",
          foreground: "rgb(var(--primary-fg) / <alpha-value>)",
        },
        accent: "rgb(var(--accent) / <alpha-value>)",
        destructive: "rgb(var(--destructive) / <alpha-value>)",
        ring: "rgb(var(--ring) / <alpha-value>)",
        brand: "rgb(var(--brand) / <alpha-value>)",

        // Палитра КЕДР
        white: "#FAF9F6",
        leaf: "#ECF39E",
        moss: "#90A955",
        grass: "#4F7730",
        forest: "#31572C",
        sunshine: "#FFC926",
        carrot: "#F96015",
        tomato: "#D52518",
        graphite: "#353535",
      },
      fontFamily: {
        sans: ['"Noto Sans"', "sans-serif"],
        mono: ['"JetBrains Mono"', "monospace"],
      },
      fontSize: {
        micro: ["0.7rem", { lineHeight: "1rem", letterSpacing: "0.16em" }],
        display: ["clamp(2.25rem, 6vw, 4.5rem)", { lineHeight: "1.1" }],
      },
      // Токены раскладки: высота шапки задаётся одной переменной --nav (см. index.css)
      height: {
        nav: "var(--nav)",
        workspace: "calc(100dvh - 2 * var(--nav))", // мобильный: шапка + нижняя навигация
        "workspace-md": "calc(100dvh - var(--nav))", // ≥ md: только шапка
        sheet: "68dvh",
        map: "clamp(22rem, 72dvh, 60rem)",
      },
      minHeight: { nav: "var(--nav)" },
      paddingBottom: { nav: "var(--nav)" },
      gridTemplateColumns: {
        header: "minmax(0, 1fr) auto minmax(0, 1fr)", // логотип | навигация строго по центру | управление
        workspace: "minmax(0, 1fr) minmax(0, 2fr)", // панель | карта
      },
      boxShadow: { soft: "0 0.5rem 2rem -0.75rem rgb(var(--foreground) / 0.3)" },
      backgroundImage: {
        "dot-grid": "radial-gradient(rgb(var(--border) / 0.45) 0.08rem, transparent 0.08rem)",
        orb: "radial-gradient(circle at center, rgb(var(--accent) / 0.85), transparent 70%)",
      },
      backgroundSize: { grid: "1.5rem 1.5rem" },
    },
  },
};
