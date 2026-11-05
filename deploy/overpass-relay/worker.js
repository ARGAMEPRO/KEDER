// Бесплатный релей к Overpass API для КЕДР.
//
// Зачем: из российских сетей overpass-api.de отдаёт 406/502, поэтому карточка
// инфраструктуры остаётся пустой. Этот воркер (Cloudflare Workers, free tier —
// 100 000 запросов в сутки) стоит снаружи блокировки и проксирует запросы.
// Перебирает несколько зеркал: overpass-api.de часто отвечает "502 at capacity",
// тогда берётся следующее.
//
// Установка (~5 минут, бесплатно):
//   1. dash.cloudflare.com -> Workers & Pages -> Create -> Worker.
//   2. Вставить этот файл, Deploy.
//   3. В .env бэкенда: OVERPASS_URLS=https://<имя>.<аккаунт>.workers.dev
//      (можно оставить и старые зеркала через запятую — релей будет первым).
//   4. Перезапустить backend. Проверка: curl -X POST <url> --data 'data=[out:json];node(1);out;'
//
// Отладка: <url>?u=https://overpass.kumi.systems/api/interpreter — закрепить одно зеркало.
//
// Ограничения: тело запроса до 100 МБ, таймаут исполнения 30 с (бесплатный план),
// поэтому тяжёлые запросы лучше не проксировать — только наши (лимит 800 объектов).

const UPSTREAMS = [
  "https://overpass-api.de/api/interpreter",
  "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
  "https://overpass.kumi.systems/api/interpreter",
];
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

export default {
  async fetch(request) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    if (!["GET", "POST"].includes(request.method)) {
      return new Response("method not allowed", { status: 405, headers: CORS });
    }
    const url = new URL(request.url);
    const body = request.method === "POST" ? await request.text() : undefined;
    const pinned = url.searchParams.get("u");
    const targets = pinned ? [pinned] : UPSTREAMS;

    let lastError = "no upstream answered";
    for (const target of targets) {
      try {
        const res = await fetch(target + (url.search || ""), {
          method: request.method,
          headers: { "User-Agent": "kedr-overpass-relay/1.0", "Content-Type": "application/x-www-form-urlencoded" },
          body,
          signal: AbortSignal.timeout(25000),
        });
        if (res.status >= 500) {
          lastError = `${target} -> ${res.status}`;  // "502 at capacity" — try the next mirror
          continue;
        }
        return new Response(res.body, {
          status: res.status,
          headers: { ...CORS, "Content-Type": res.headers.get("Content-Type") || "application/json" },
        });
      } catch (e) {
        lastError = `${target} -> ${String(e)}`;
      }
    }
    return new Response(JSON.stringify({ error: "all upstreams failed", detail: lastError }), {
      status: 502, headers: { ...CORS, "Content-Type": "application/json" },
    });
  },
};
