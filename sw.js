// IPO 동향 분석 서비스워커: 앱 설치용. 항상 네트워크 먼저(최신 데이터), 끊겼을 때만 마지막으로 본 화면·데이터 표시
const CACHE = "ipo-v3";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-192.png"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;   // 차트 라이브러리·폰트 등 외부 주소는 그대로
  if (url.pathname.includes("/data/prices/")) return;                         // 종목별 시세는 저장하지 않음(용량)
  e.respondWith(fetch(e.request).then((r) => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request, { ignoreSearch: true }).then((r) => r || (e.request.mode === "navigate" ? caches.match("index.html") : Response.error()))));
});
