"""
Operator dashboard — Beastmode aesthetic.

Mounted on the same Flask app as the x402 proxy so a single web process
serves both protocol traffic and the command UI. The dashboard is read-only
— it surfaces live ledger state, harvested bounty targets, generated pages,
and per-vertical gap statistics. It never mutates engine state.

Authentication — FAILURE-CLOSED
-------------------------------
The dashboard routes are registered ONLY if DASHBOARD_AUTH_TOKEN is set
to a non-empty value. Without the env var, the routes do not exist at
all (404 from Flask's default handler). With the env var, every
dashboard route requires `Authorization: Bearer <token>` with constant-
time comparison.

The HTML page accepts a `?token=<>` query-string convenience on first
load — the page strips the token from the URL via history.replaceState
and stores it in sessionStorage for subsequent fetches. API endpoints
require the header; the query-string shortcut does not extend to them.

Per BB7_DESIGN.md §9.6, the dashboard cannot surface paid-placement
records or any data that maps SML to specific external domains. The
auth gate is the first defense; the audit_no_secret_leak test is the
second.

Aesthetic mandate (strict): pure black backgrounds (#000000 / #050505 /
#0a0a0a), neon accents only (cyan / magenta / electric green / amber),
monospace type, sharp corners, terminal-grid layout. Zero default browser
styling, zero corporate gray, zero rounded corners.

Routes
------
  GET /dashboard                   — HTML command center
  GET /api/dashboard/state         — proxy ledger + per-vertical summary
  GET /api/dashboard/bounty/<v>    — bounty_targets.json for vertical v
  GET /api/dashboard/pages/<v>     — list of generated pages for vertical v
"""

import functools
import hmac
import json
import logging
import os

from flask import Flask, Response, abort, jsonify, request

logger = logging.getLogger("sml-beast.dashboard")


VERTICALS = ("mastersheets", "xrpl")  # output dir names, not worker.vertical keys


def _safe_vertical(v: str) -> str:
    if v not in VERTICALS:
        abort(404)
    return v


def _read_bounty(output_root: str, vertical: str) -> dict:
    path = os.path.join(output_root, vertical, "bounty_targets.json")
    if not os.path.isfile(path):
        return {"vertical": vertical, "total_domains": 0, "targets": [], "missing": True}
    with open(path) as f:
        return json.load(f)


def _list_pages(output_root: str, vertical: str) -> list[dict]:
    base = os.path.join(output_root, vertical)
    if not os.path.isdir(base):
        return []
    out: list[dict] = []
    for entry in sorted(os.listdir(base)):
        page_dir = os.path.join(base, entry)
        mdx = os.path.join(page_dir, "page.mdx")
        if os.path.isfile(mdx):
            stat = os.stat(mdx)
            out.append(
                {
                    "slug": entry,
                    "mtime": int(stat.st_mtime),
                    "size_bytes": stat.st_size,
                }
            )
    out.sort(key=lambda p: -p["mtime"])
    return out


def _make_auth_decorator(token: str, allow_query_param_on_html: bool = False):
    """Build a route-level auth decorator bound to a specific token.

    Bearer header is the canonical auth path for both HTML + JSON routes.
    `allow_query_param_on_html=True` opens an additional `?token=<>` route
    used only by the HTML page for first-load convenience — the page
    strips the token from the URL and moves it into sessionStorage."""

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            provided = ""
            h = request.headers.get("Authorization", "")
            if h.startswith("Bearer "):
                provided = h[7:].strip()
            if not provided and allow_query_param_on_html:
                provided = (request.args.get("token") or "").strip()
            if not provided or not hmac.compare_digest(provided, token):
                abort(401)
            return f(*args, **kwargs)

        return wrapper

    return decorator


def register_dashboard(app: Flask, output_root: str, ledger_ref: tuple) -> None:
    """Attach the dashboard routes to an existing Flask app.

    FAILURE-CLOSED: if DASHBOARD_AUTH_TOKEN is not set in the environment
    the routes are NOT registered — Flask returns its default 404 for
    /dashboard and /api/dashboard/*. There is no soft-fallback path that
    serves the dashboard open. The operator must configure the token
    before the dashboard exists.

    ledger_ref is a (lock, dict) tuple — the proxy's in-process ledger.
    Passing a reference (not a copy) lets the dashboard see live state
    without any cross-thread polling layer."""

    token = os.environ.get("DASHBOARD_AUTH_TOKEN", "").strip()
    if not token:
        logger.warning(
            "DASHBOARD_AUTH_TOKEN unset — dashboard routes will NOT be "
            "registered. Set this env var to a cryptographically-random "
            "value (e.g. `openssl rand -hex 32`) to enable the dashboard."
        )
        return

    require_api_auth = _make_auth_decorator(token, allow_query_param_on_html=False)
    require_html_auth = _make_auth_decorator(token, allow_query_param_on_html=True)

    ledger_lock, ledger = ledger_ref

    @app.route("/api/dashboard/state", methods=["GET"])
    @require_api_auth
    def state():
        with ledger_lock:
            wallets = [
                {
                    "wallet": w,
                    "calls": e["calls"],
                    "paid_usdc": round(e["paid_usdc"], 6),
                    "last_ts": int(e["last_ts"]) if e["last_ts"] else 0,
                }
                for w, e in sorted(ledger.items())
            ]
            total_calls = sum(e["calls"] for e in ledger.values())

        verticals = []
        for v in VERTICALS:
            bounty = _read_bounty(output_root, v)
            pages = _list_pages(output_root, v)
            verticals.append(
                {
                    "vertical": v,
                    "bounty_domains": bounty.get("total_domains", 0),
                    "serps_ingested": bounty.get("total_serps_ingested", 0),
                    "pages_generated": len(pages),
                    "latest_page_mtime": pages[0]["mtime"] if pages else 0,
                }
            )

        return jsonify(
            {
                "proxy": {"total_calls": total_calls, "wallets": wallets},
                "verticals": verticals,
                "output_root": output_root,
            }
        )

    @app.route("/api/dashboard/bounty/<vertical>", methods=["GET"])
    @require_api_auth
    def bounty(vertical: str):
        v = _safe_vertical(vertical)
        return jsonify(_read_bounty(output_root, v))

    @app.route("/api/dashboard/pages/<vertical>", methods=["GET"])
    @require_api_auth
    def pages(vertical: str):
        v = _safe_vertical(vertical)
        return jsonify({"vertical": v, "pages": _list_pages(output_root, v)})

    @app.route("/dashboard", methods=["GET"])
    @require_html_auth
    def dashboard():
        return Response(_DASHBOARD_HTML, mimetype="text/html")

    logger.info("Dashboard mounted at /dashboard (auth gate active)")


# ── single-file Beastmode UI ─────────────────────────────────────────────────

_DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>SML.BEAST.OPS // command center</title>
<meta name="robots" content="noindex">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-0: #000000;
    --bg-1: #050505;
    --bg-2: #0a0a0a;
    --neon-cyan:    #00ffff;
    --neon-magenta: #ff00ff;
    --neon-green:   #00ff66;
    --neon-amber:   #ffb000;
    --text:         #d9ffff;
    --text-dim:     #4dffaa;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; border-radius: 0 !important; }
  html, body {
    background: var(--bg-0);
    color: var(--text);
    font-family: "JetBrains Mono", "Menlo", "Consolas", monospace;
    font-size: 13px;
    line-height: 1.45;
    min-height: 100vh;
    letter-spacing: 0.01em;
  }
  body {
    background:
      radial-gradient(ellipse at 50% -10%, rgba(0,255,255,0.08), transparent 60%),
      radial-gradient(ellipse at 100% 100%, rgba(255,0,255,0.05), transparent 60%),
      var(--bg-0);
    padding: 16px 24px 80px;
  }
  /* CRT scanline overlay */
  body::before {
    content: "";
    position: fixed; inset: 0; pointer-events: none; z-index: 999;
    background: repeating-linear-gradient(
      to bottom, rgba(255,255,255,0.015) 0 1px, transparent 1px 3px);
  }
  header {
    border: 1px solid var(--neon-cyan);
    padding: 10px 14px;
    margin-bottom: 16px;
    background: var(--bg-1);
    box-shadow: 0 0 12px rgba(0,255,255,0.25), inset 0 0 18px rgba(0,255,255,0.05);
  }
  header h1 {
    font-size: 14px;
    font-weight: 700;
    color: var(--neon-cyan);
    text-shadow: 0 0 6px rgba(0,255,255,0.6);
    letter-spacing: 0.12em;
  }
  header .sub {
    color: var(--neon-magenta);
    font-size: 11px;
    margin-top: 4px;
    text-shadow: 0 0 6px rgba(255,0,255,0.5);
  }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 1024px) { .row { grid-template-columns: 1fr; } }
  .panel {
    border: 1px solid var(--neon-cyan);
    background: var(--bg-1);
    padding: 12px 14px;
    margin-bottom: 16px;
    box-shadow: 0 0 8px rgba(0,255,255,0.15), inset 0 0 12px rgba(0,0,0,0.6);
  }
  .panel.magenta { border-color: var(--neon-magenta); box-shadow: 0 0 8px rgba(255,0,255,0.18), inset 0 0 12px rgba(0,0,0,0.6); }
  .panel.green   { border-color: var(--neon-green);   box-shadow: 0 0 8px rgba(0,255,102,0.18), inset 0 0 12px rgba(0,0,0,0.6); }
  .panel h2 {
    font-size: 11px;
    font-weight: 700;
    color: var(--neon-cyan);
    text-shadow: 0 0 4px rgba(0,255,255,0.5);
    letter-spacing: 0.18em;
    padding-bottom: 6px;
    margin-bottom: 8px;
    border-bottom: 1px dashed var(--neon-cyan);
  }
  .panel.magenta h2 { color: var(--neon-magenta); border-bottom-color: var(--neon-magenta); text-shadow: 0 0 4px rgba(255,0,255,0.5); }
  .panel.green   h2 { color: var(--neon-green);   border-bottom-color: var(--neon-green);   text-shadow: 0 0 4px rgba(0,255,102,0.5); }
  .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; }
  .stat {
    border: 1px solid var(--text-dim);
    padding: 8px 10px;
    background: var(--bg-2);
  }
  .stat .label { color: var(--text-dim); font-size: 10px; letter-spacing: 0.14em; text-transform: uppercase; }
  .stat .value { color: var(--neon-green); font-size: 18px; font-weight: 700; text-shadow: 0 0 5px rgba(0,255,102,0.45); }
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  th, td {
    text-align: left;
    padding: 5px 8px;
    border-bottom: 1px dashed rgba(0,255,255,0.18);
  }
  th {
    color: var(--neon-cyan);
    text-shadow: 0 0 3px rgba(0,255,255,0.5);
    letter-spacing: 0.14em;
    font-size: 10px;
    text-transform: uppercase;
    border-bottom: 1px solid var(--neon-cyan);
  }
  td.domain { color: var(--neon-magenta); text-shadow: 0 0 3px rgba(255,0,255,0.4); }
  td.num    { color: var(--neon-green);   text-shadow: 0 0 3px rgba(0,255,102,0.4); text-align: right; }
  td.class  { color: var(--neon-amber); text-shadow: 0 0 3px rgba(255,176,0,0.45); text-transform: uppercase; font-size: 10px; letter-spacing: 0.12em; }
  tr:hover td { background: rgba(0,255,255,0.04); }
  .empty { color: var(--neon-amber); font-style: italic; text-shadow: 0 0 3px rgba(255,176,0,0.5); padding: 8px 0; }
  .pulse {
    display: inline-block;
    width: 8px; height: 8px;
    background: var(--neon-green);
    box-shadow: 0 0 8px var(--neon-green), 0 0 16px var(--neon-green);
    margin-right: 8px;
    animation: pulse 1.4s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1.0; transform: scale(1.0); }
    50%      { opacity: 0.4; transform: scale(0.75); }
  }
  footer {
    color: var(--text-dim);
    font-size: 10px;
    letter-spacing: 0.12em;
    margin-top: 12px;
    padding-top: 8px;
    border-top: 1px dashed var(--text-dim);
  }
  .wallet-line { font-size: 11px; padding: 3px 0; color: var(--text); }
  .wallet-line .wlt { color: var(--neon-magenta); }
  .wallet-line .meta { color: var(--text-dim); }
</style>
</head>
<body>

<header>
  <h1><span class="pulse"></span>SML.BEAST.OPS // COMMAND CENTER</h1>
  <div class="sub">x402 facilitator-proxy &middot; SERP-gap engine &middot; M2M bounty harvest</div>
</header>

<div class="panel">
  <h2>[PROXY] // LIVE STATE</h2>
  <div class="stat-grid">
    <div class="stat"><div class="label">total calls</div><div class="value" id="stat-calls">--</div></div>
    <div class="stat"><div class="label">active wallets</div><div class="value" id="stat-wallets">--</div></div>
    <div class="stat"><div class="label">output root</div><div class="value" style="font-size: 11px; word-break: break-all;" id="stat-root">--</div></div>
  </div>
  <div style="margin-top: 10px;" id="wallet-list"></div>
</div>

<div class="row">
  <div class="panel magenta" id="vert-mastersheets">
    <h2>[VERTICAL] // MASTERSHEETS</h2>
    <div class="stat-grid">
      <div class="stat"><div class="label">bounty domains</div><div class="value vert-bounty">--</div></div>
      <div class="stat"><div class="label">SERPs ingested</div><div class="value vert-serps">--</div></div>
      <div class="stat"><div class="label">pages generated</div><div class="value vert-pages">--</div></div>
    </div>
  </div>
  <div class="panel green" id="vert-xrpl">
    <h2>[VERTICAL] // XRPL / X402</h2>
    <div class="stat-grid">
      <div class="stat"><div class="label">bounty domains</div><div class="value vert-bounty">--</div></div>
      <div class="stat"><div class="label">SERPs ingested</div><div class="value vert-serps">--</div></div>
      <div class="stat"><div class="label">pages generated</div><div class="value vert-pages">--</div></div>
    </div>
  </div>
</div>

<div class="row">
  <div class="panel magenta">
    <h2>[BOUNTY] // MASTERSHEETS // TOP TARGETS</h2>
    <table id="tbl-bounty-mastersheets">
      <thead><tr><th>domain</th><th>class</th><th class="num">freq</th><th class="num">weight</th><th class="num">priority</th></tr></thead>
      <tbody><tr><td colspan="5" class="empty">awaiting harvest...</td></tr></tbody>
    </table>
  </div>
  <div class="panel green">
    <h2>[BOUNTY] // XRPL / X402 // TOP TARGETS</h2>
    <table id="tbl-bounty-xrpl">
      <thead><tr><th>domain</th><th>class</th><th class="num">freq</th><th class="num">weight</th><th class="num">priority</th></tr></thead>
      <tbody><tr><td colspan="5" class="empty">awaiting harvest...</td></tr></tbody>
    </table>
  </div>
</div>

<footer>
  refresh interval: 5s &middot; last pull: <span id="last-pull">--</span> &middot;
  this dashboard is READ-ONLY &middot; nothing here mutates engine state
</footer>

<script>
const VERTICALS = ["mastersheets", "xrpl"];

// Auth handshake: ?token=<> on first load -> sessionStorage -> Bearer header
// for all subsequent fetches. The URL is stripped so the token doesn't leak
// into browser history, server logs, or shoulder-surfing screenshots.
(function lockAuth() {
  const url = new URL(window.location.href);
  const t = url.searchParams.get("token");
  if (t) {
    sessionStorage.setItem("beast_dash_token", t);
    url.searchParams.delete("token");
    window.history.replaceState({}, "", url.toString());
  }
})();

function authHeaders() {
  const t = sessionStorage.getItem("beast_dash_token");
  return t ? { "Authorization": "Bearer " + t } : {};
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now()/1000 - ts));
  if (s < 60)   return s + "s ago";
  if (s < 3600) return Math.floor(s/60) + "m ago";
  return Math.floor(s/3600) + "h ago";
}

async function pull() {
  try {
    const r = await fetch("/api/dashboard/state", {
      cache: "no-store", headers: authHeaders() });
    if (r.status === 401) {
      sessionStorage.removeItem("beast_dash_token");
      document.body.innerHTML = '<div style="padding:40px;color:#ff00ff;font-family:JetBrains Mono,monospace;text-shadow:0 0 6px #ff00ff;">[401] // session expired. reload with ?token=&lt;your-token&gt;</div>';
      return;
    }
    const s = await r.json();

    document.getElementById("stat-calls").textContent   = s.proxy.total_calls;
    document.getElementById("stat-wallets").textContent = s.proxy.wallets.length;
    document.getElementById("stat-root").textContent    = s.output_root;

    const wlt = document.getElementById("wallet-list");
    if (!s.proxy.wallets.length) {
      wlt.innerHTML = '<div class="empty">no wallets active yet — orchestrator may be starting</div>';
    } else {
      wlt.innerHTML = s.proxy.wallets.map(w =>
        `<div class="wallet-line">> <span class="wlt">${w.wallet}</span>
         <span class="meta">calls=${w.calls} paid=${w.paid_usdc} USDC last=${ago(w.last_ts)}</span></div>`
      ).join("");
    }

    for (const v of s.verticals) {
      const id = v.vertical === "xrpl" ? "vert-xrpl" : "vert-mastersheets";
      const panel = document.getElementById(id);
      if (!panel) continue;
      panel.querySelector(".vert-bounty").textContent = v.bounty_domains;
      panel.querySelector(".vert-serps").textContent  = v.serps_ingested;
      panel.querySelector(".vert-pages").textContent  = v.pages_generated;
    }
  } catch (e) {
    console.error("state pull failed", e);
  }

  for (const v of VERTICALS) {
    try {
      const r = await fetch(`/api/dashboard/bounty/${v}`, {
        cache: "no-store", headers: authHeaders() });
      if (r.status === 401) continue;
      const b = await r.json();
      const tbl = document.getElementById(`tbl-bounty-${v}`);
      if (!tbl) continue;
      const tbody = tbl.querySelector("tbody");
      if (b.missing || !b.targets || !b.targets.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="empty">awaiting harvest...</td></tr>';
        continue;
      }
      tbody.innerHTML = b.targets.slice(0, 15).map(t =>
        `<tr>
           <td class="domain">${t.domain}</td>
           <td class="class">${t.class}</td>
           <td class="num">${t.frequency}</td>
           <td class="num">${t.class_weight}</td>
           <td class="num">${t.priority_score}</td>
         </tr>`
      ).join("");
    } catch (e) {
      console.error(`bounty pull ${v} failed`, e);
    }
  }

  document.getElementById("last-pull").textContent = new Date().toLocaleTimeString();
}

pull();
setInterval(pull, 5000);
</script>
</body>
</html>
"""
