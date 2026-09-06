/* ============================================================
 * QCS 质量协同系统 · Service Worker
 * 策略:
 *   - 静态资源(本目录文件): 缓存优先, 后台更新(stale-while-revalidate)
 *   - /api/ 接口(含跨域): 永不缓存, 全部走网络, 避免登录/业务数据过期
 *   - 页面导航失败(离线): 回退到缓存的 index.html
 * 更新版本时把 CACHE_VERSION 号 +1, 旧缓存会在 activate 阶段自动清理
 * ============================================================ */

const CACHE_VERSION = "v2";
const CACHE_NAME = "qcs-static-" + CACHE_VERSION;

/* 预缓存的核心静态资源(相对 SW 所在目录, 部署在任意子路径都可用) */
const PRECACHE_URLS = [
  "./",
  "./index.html",
  "./app.js",
  "./style.css",
  "./manifest.json",
  "./icons/icon-192.png",
  "./icons/icon-512.png",
  "./icons/apple-touch-icon.png"
];

/* 安装: 预缓存核心资源, 立即激活新 SW */
self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

/* 激活: 清理旧版本缓存, 立即接管所有页面 */
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

/* 是否为 API 请求(路径以 /api/ 开头, 含跨域接口) */
function isApiRequest(url) {
  return url.pathname.includes("/api/");
}

self.addEventListener("fetch", (event) => {
  const req = event.request;

  /* 非 GET(POST 提交/登录等)一律直连网络, 不做任何缓存 */
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  /* API 请求: 永不缓存, 全部走网络 */
  if (isApiRequest(url)) return;

  /* 静态资源: 缓存优先 + 后台更新 */
  event.respondWith(
    caches.match(req).then((cached) => {
      const network = fetch(req)
        .then((resp) => {
          /* 只缓存成功且基本有效的响应 */
          if (resp && resp.ok && (resp.type === "basic" || resp.type === "cors")) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(req, clone));
          }
          return resp;
        })
        .catch(() => cached);

      /* 命中缓存立即返回, 同时后台更新; 未命中等网络 */
      return cached || network;
    }).catch(() => {
      /* 导航请求彻底失败(离线) → 回退缓存的首页 */
      if (req.mode === "navigate") {
        return caches.match("./index.html");
      }
      return Response.error();
    })
  );
});
