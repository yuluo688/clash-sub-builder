/**
 * clash-sub-builder Cloudflare Worker
 * 模式 A：从 GitHub Raw 拉取 output/all.yaml 并分发
 *
 * 环境变量：
 *   GITHUB_RAW_URL  - all.yaml 的 raw 地址（必填）
 *   GITHUB_STATS_URL - stats.json 的 raw 地址（可选）
 *   SUB_TOKEN       - 可选访问令牌；未设置则公开
 *   PROFILE_NAME    - 客户端订阅显示名（默认 clash-sub-builder）
 *   PROFILE_UPDATE_INTERVAL - 建议更新间隔小时（默认 4）
 *   SUB_TOTAL_BYTES / SUB_UPLOAD_BYTES / SUB_DOWNLOAD_BYTES / SUB_EXPIRE
 *                   - subscription-userinfo（默认 24TB / 0 / 0 / 2099-12-31）
 */

const DEFAULT_CACHE_SECONDS = 300;
/** 24 TiB */
const DEFAULT_TOTAL_BYTES = 24 * 1024 * 1024 * 1024 * 1024;
/** 2099-12-31 00:00:00 UTC */
const DEFAULT_EXPIRE_TS = 4102329600;

function utf8ToBase64(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

/** Clash / Clash Verge 订阅卡片：名称、流量条、到期日、更新周期 */
function profileMetaHeaders(env) {
  const name = (env.PROFILE_NAME || "clash-sub-builder").trim() || "clash-sub-builder";
  const updateHours = String(env.PROFILE_UPDATE_INTERVAL || "4");
  const total = Number(env.SUB_TOTAL_BYTES || DEFAULT_TOTAL_BYTES);
  const upload = Number(env.SUB_UPLOAD_BYTES || 0);
  const download = Number(env.SUB_DOWNLOAD_BYTES || 0);
  const expire = Number(env.SUB_EXPIRE || DEFAULT_EXPIRE_TS);
  const safeName = name.replace(/["\\\r\n]/g, "");

  return {
    "content-disposition": `attachment; filename="${safeName}.yaml"`,
    "profile-title": `base64:${utf8ToBase64(name)}`,
    "profile-update-interval": updateHours,
    "subscription-userinfo": `upload=${upload}; download=${download}; total=${total}; expire=${expire}`,
  };
}

function unauthorized() {
  return new Response(JSON.stringify({ error: "unauthorized" }), {
    status: 401,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

function checkToken(request, env) {
  const required = env.SUB_TOKEN;
  if (!required) return true;
  const url = new URL(request.url);
  const q = url.searchParams.get("token");
  const header = request.headers.get("X-Sub-Token");
  const auth = request.headers.get("Authorization");
  const bearer =
    auth && auth.toLowerCase().startsWith("bearer ")
      ? auth.slice(7).trim()
      : null;
  return q === required || header === required || bearer === required;
}

async function fetchUpstream(url, request) {
  const headers = {
    "User-Agent": "clash-sub-builder-worker/1.0",
    Accept: "*/*",
  };
  // 透传条件请求，利于 GitHub / CDN 缓存
  const inm = request.headers.get("If-None-Match");
  if (inm) headers["If-None-Match"] = inm;
  const ims = request.headers.get("If-Modified-Since");
  if (ims) headers["If-Modified-Since"] = ims;

  return fetch(url, {
    headers,
    cf: { cacheTtl: DEFAULT_CACHE_SECONDS, cacheEverything: true },
  });
}

function withCacheHeaders(resp, extra = {}) {
  const headers = new Headers(resp.headers);
  headers.set(
    "Cache-Control",
    `public, max-age=${DEFAULT_CACHE_SECONDS}, s-maxage=${DEFAULT_CACHE_SECONDS}`
  );
  // 透传 ETag / Last-Modified
  const etag = resp.headers.get("ETag");
  if (etag) headers.set("ETag", etag);
  const lm = resp.headers.get("Last-Modified");
  if (lm) headers.set("Last-Modified", lm);
  for (const [k, v] of Object.entries(extra)) {
    headers.set(k, v);
  }
  return headers;
}

async function handleSub(request, env) {
  if (!checkToken(request, env)) return unauthorized();
  const rawUrl = env.GITHUB_RAW_URL;
  if (!rawUrl) {
    return new Response(
      JSON.stringify({
        error: "GITHUB_RAW_URL not configured",
        hint: "Set worker var to https://raw.githubusercontent.com/<user>/<repo>/<branch>/output/all.yaml",
      }),
      { status: 500, headers: { "content-type": "application/json; charset=utf-8" } }
    );
  }

  const meta = profileMetaHeaders(env);
  const upstream = await fetchUpstream(rawUrl, request);
  if (upstream.status === 304) {
    return new Response(null, {
      status: 304,
      headers: withCacheHeaders(upstream, meta),
    });
  }
  if (!upstream.ok) {
    return new Response(
      JSON.stringify({
        error: "upstream fetch failed",
        status: upstream.status,
      }),
      {
        status: 502,
        headers: { "content-type": "application/json; charset=utf-8" },
      }
    );
  }

  const body = await upstream.text();
  // 本地计算弱 ETag，避免上游无 ETag 时客户端无法缓存协商
  const etag =
    upstream.headers.get("ETag") ||
    `"${await sha256Short(body)}"`;

  const ifNoneMatch = request.headers.get("If-None-Match");
  if (ifNoneMatch && ifNoneMatch === etag) {
    return new Response(null, {
      status: 304,
      headers: withCacheHeaders(upstream, { ETag: etag, ...meta }),
    });
  }

  return new Response(body, {
    status: 200,
    headers: withCacheHeaders(upstream, {
      ETag: etag,
      "content-type": "text/yaml; charset=utf-8",
      ...meta,
    }),
  });
}

async function sha256Short(text) {
  const data = new TextEncoder().encode(text);
  const hash = await crypto.subtle.digest("SHA-256", data);
  const bytes = new Uint8Array(hash);
  let hex = "";
  for (let i = 0; i < 8; i++) {
    hex += bytes[i].toString(16).padStart(2, "0");
  }
  return hex;
}

async function loadStats(env) {
  if (!env.GITHUB_STATS_URL) {
    // 尝试从 all.yaml 头部注释推断
    if (!env.GITHUB_RAW_URL) return null;
    try {
      const r = await fetch(env.GITHUB_RAW_URL, {
        headers: { "User-Agent": "clash-sub-builder-worker/1.0" },
      });
      if (!r.ok) return null;
      const text = await r.text();
      const nodesMatch = text.match(/#\s*Nodes:\s*(\d+)/i);
      const updatedMatch = text.match(/#\s*Updated:\s*(\S+)/i);
      return {
        status: "ok",
        updated_at: updatedMatch ? updatedMatch[1] : null,
        nodes: nodesMatch ? Number(nodesMatch[1]) : null,
        total: nodesMatch ? Number(nodesMatch[1]) : null,
        countries: {},
      };
    } catch {
      return null;
    }
  }
  try {
    const r = await fetch(env.GITHUB_STATS_URL, {
      headers: { "User-Agent": "clash-sub-builder-worker/1.0" },
    });
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

async function handleHealth(request, env) {
  if (!checkToken(request, env)) return unauthorized();
  const stats = (await loadStats(env)) || {
    status: "degraded",
    updated_at: null,
    nodes: null,
  };
  return new Response(
    JSON.stringify(
      {
        status: stats.status || "ok",
        updated_at: stats.updated_at || null,
        nodes: stats.nodes ?? stats.total ?? null,
      },
      null,
      2
    ),
    { headers: { "content-type": "application/json; charset=utf-8" } }
  );
}

async function handleStats(request, env) {
  if (!checkToken(request, env)) return unauthorized();
  const stats = (await loadStats(env)) || {
    total: 0,
    countries: {},
  };
  return new Response(
    JSON.stringify(
      {
        total: stats.total ?? stats.nodes ?? 0,
        countries: stats.countries || {},
        updated_at: stats.updated_at || null,
      },
      null,
      2
    ),
    { headers: { "content-type": "application/json; charset=utf-8" } }
  );
}

function handleIndex(env) {
  const hasToken = Boolean(env.SUB_TOKEN);
  const html = `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>clash-sub-builder</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; color: #111; }
    code { background: #f4f4f5; padding: 2px 6px; border-radius: 4px; }
    a { color: #2563eb; }
    .card { border: 1px solid #e5e7eb; border-radius: 12px; padding: 20px; }
  </style>
</head>
<body>
  <div class="card">
    <h1>clash-sub-builder</h1>
    <p>公开代理订阅聚合分发（只读）。</p>
    <ul>
      <li>订阅：<code>/sub${hasToken ? "?token=***" : ""}</code></li>
      <li>健康：<code>/health</code></li>
      <li>统计：<code>/stats</code></li>
    </ul>
    <p>Auth: <strong>${hasToken ? "token required" : "public"}</strong></p>
  </div>
</body>
</html>`;
  return new Response(html, {
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    try {
      if (path === "/" || path === "") return handleIndex(env);
      if (path === "/sub") return handleSub(request, env);
      if (path === "/health") return handleHealth(request, env);
      if (path === "/stats") return handleStats(request, env);
      return new Response(JSON.stringify({ error: "not found" }), {
        status: 404,
        headers: { "content-type": "application/json; charset=utf-8" },
      });
    } catch (e) {
      return new Response(
        JSON.stringify({ error: "internal", message: String(e) }),
        {
          status: 500,
          headers: { "content-type": "application/json; charset=utf-8" },
        }
      );
    }
  },
};