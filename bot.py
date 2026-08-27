import os
import time
import datetime as dt
from threading import Thread, Lock

import requests
from flask import Flask, jsonify, send_from_directory

try:
    from flask_cors import CORS
except Exception:
    CORS = None


# ============================================================
# KETS APP — SOURCE-BACKEND CONNECTOR
#
# IMPORTANT:
# KETS APP DOES NOT GENERATE A SECOND TRADING STRATEGY.
#
# Data flow:
#   ORIGINAL TRADING BOT
#          ↓
#   /api/status
#   /api/market
#   /api/signals
#          ↓
#       KETS APP
#          ↓
#      Web dashboard
#
# The original trading bot remains the source of truth for:
# - market data
# - BUY / SELL signals
# - score
# - entry
# - take profit
# - stop loss
# - signal history
#
# KETS only fetches, caches and serves that data.
#
# Render:
# - Flask web server
# - background polling
# - self-awake heartbeat
# ============================================================


app = Flask(__name__)

if CORS:
    try:
        CORS(app)
    except Exception:
        pass


# ============================================================
# CONFIGURATION
# ============================================================

SOURCE_API = (
    os.environ.get("ORIGINAL_BOT_API_URL")
    or os.environ.get("SOURCE_BOT_API_URL")
    or "https://my-btc-bot-l0xm.onrender.com"
).rstrip("/")

POLL_SECONDS = int(
    os.environ.get("KETS_POLL_SECONDS", "120")
)

HEARTBEAT_SECONDS = int(
    os.environ.get("KETS_HEARTBEAT_SECONDS", "300")
)

SOURCE_TIMEOUT = int(
    os.environ.get("KETS_SOURCE_TIMEOUT", "25")
)

PUBLIC_URL = (
    os.environ.get("KETS_PUBLIC_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")


# ============================================================
# STATE
# ============================================================

LOCK = Lock()

SOURCE_STATE = {
    "connected": False,
    "status": "starting",
    "last_success": None,
    "last_error": None,
    "source_api": SOURCE_API,
}

MARKET_DATA = {}
SIGNALS = []

LAST_SOURCE_STATUS = {}
LAST_SOURCE_MARKET = {}
LAST_SOURCE_SIGNALS = []

last_poll = None
next_poll = None
engine_started = False
worker_started = False


# ============================================================
# TIME
# ============================================================

def get_eat_time():
    return (
        dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(hours=3)
    )


def iso_now():
    return get_eat_time().isoformat()


# ============================================================
# SAFE HTTP
# ============================================================

def get_json(path):
    url = f"{SOURCE_API}{path}"

    response = requests.get(
        url,
        timeout=SOURCE_TIMEOUT,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "KETS-App-Connector/1.0",
        },
    )

    response.raise_for_status()

    return response.json()


# ============================================================
# NORMALIZATION
#
# The original backend already exposes the API shape KETS needs.
# These functions also tolerate small variations in field names.
# ============================================================

def normalize_market_payload(payload):
    if not isinstance(payload, dict):
        return {}

    markets = payload.get("markets")

    if isinstance(markets, dict):
        return markets

    if isinstance(markets, list):
        result = {}

        for item in markets:
            if not isinstance(item, dict):
                continue

            asset = (
                item.get("asset")
                or item.get("market")
                or item.get("symbol")
            )

            if asset:
                result[str(asset)] = item

        return result

    return {}


def normalize_signal_payload(payload):
    if not isinstance(payload, dict):
        return []

    signals = payload.get("signals")

    if isinstance(signals, list):
        return signals

    if isinstance(payload.get("data"), list):
        return payload["data"]

    return []


def clean_signal(signal):
    if not isinstance(signal, dict):
        return None

    result = dict(signal)

    # Make common aliases consistent for the KETS frontend.
    if "direction" not in result:
        result["direction"] = (
            result.get("signal")
            or result.get("side")
            or result.get("action")
        )

    if "entry" not in result:
        result["entry"] = (
            result.get("price")
            or result.get("entry_price")
        )

    if "take_profit" not in result:
        result["take_profit"] = (
            result.get("tp")
            or result.get("takeProfit")
        )

    if "stop_loss" not in result:
        result["stop_loss"] = (
            result.get("sl")
            or result.get("stopLoss")
        )

    if "timestamp" not in result:
        result["timestamp"] = (
            result.get("time")
            or result.get("created_at")
            or result.get("createdAt")
        )

    return result


# ============================================================
# SOURCE POLLER
# ============================================================

def poll_source():
    global last_poll
    global next_poll
    global LAST_SOURCE_STATUS
    global LAST_SOURCE_MARKET
    global LAST_SOURCE_SIGNALS

    started = time.time()

    with LOCK:
        last_poll = iso_now()

    try:
        # ------------------------------------------------------
        # 1. STATUS
        # ------------------------------------------------------
        status_payload = get_json("/api/status")

        # ------------------------------------------------------
        # 2. MARKET DATA
        # ------------------------------------------------------
        market_payload = get_json("/api/market")

        # ------------------------------------------------------
        # 3. SIGNALS
        # ------------------------------------------------------
        signals_payload = get_json("/api/signals")

        markets = normalize_market_payload(
            market_payload
        )

        raw_signals = normalize_signal_payload(
            signals_payload
        )

        normalized_signals = []

        for signal in raw_signals:
            cleaned = clean_signal(signal)

            if cleaned is not None:
                normalized_signals.append(
                    cleaned
                )

        now = iso_now()

        with LOCK:
            LAST_SOURCE_STATUS = (
                status_payload
                if isinstance(status_payload, dict)
                else {}
            )

            LAST_SOURCE_MARKET = (
                markets
            )

            LAST_SOURCE_SIGNALS = (
                normalized_signals
            )

            MARKET_DATA.clear()
            MARKET_DATA.update(markets)

            SIGNALS.clear()
            SIGNALS.extend(
                normalized_signals
            )

            SOURCE_STATE["connected"] = True
            SOURCE_STATE["status"] = "connected"
            SOURCE_STATE["last_success"] = now
            SOURCE_STATE["last_error"] = None

        print(
            f"✅ SOURCE CONNECTED | "
            f"markets={len(markets)} | "
            f"signals={len(normalized_signals)}"
        )

        source_status = (
            status_payload.get("status")
            if isinstance(status_payload, dict)
            else None
        )

        print(
            f"📡 Original bot status: "
            f"{source_status or 'unknown'}"
        )

        for asset, market in markets.items():
            if isinstance(market, dict):
                print(
                    f"📊 {asset}: "
                    f"price={market.get('price')} | "
                    f"signal={market.get('signal')} | "
                    f"score={market.get('score')}"
                )

    except Exception as exc:

        error = str(exc)[:500]

        with LOCK:
            SOURCE_STATE["connected"] = False
            SOURCE_STATE["status"] = "source_unavailable"
            SOURCE_STATE["last_error"] = error

        print(
            f"⚠️ SOURCE FETCH ERROR: {error}"
        )

    elapsed = time.time() - started

    sleep_for = max(
        1,
        POLL_SECONDS - elapsed
    )

    with LOCK:
        next_poll = (
            get_eat_time()
            + dt.timedelta(seconds=sleep_for)
        ).isoformat()


def source_worker():
    global engine_started

    engine_started = True

    print(
        "🚀 KETS SOURCE CONNECTOR ONLINE"
    )

    print(
        f"📡 Source: {SOURCE_API}"
    )

    print(
        f"🔄 Poll interval: {POLL_SECONDS}s"
    )

    print(
        "🧠 Strategy generation: DISABLED"
    )

    print(
        "🎯 Original trading bot = SOURCE OF TRUTH"
    )

    while True:

        poll_source()

        with LOCK:
            next_time = next_poll

        print(
            f"⏱️ Next source fetch: "
            f"{next_time}"
        )

        # Sleep until the next scheduled poll. poll_source() already
        # calculated next_poll from the actual elapsed request time.
        time.sleep(max(1, POLL_SECONDS))


# ============================================================
# SELF-AWAKE
# ============================================================

def public_url():
    return (
        PUBLIC_URL
        or os.environ.get("RENDER_EXTERNAL_URL")
        or ""
    ).rstrip("/")


def self_awake():

    while True:

        url = public_url()

        if url:

            try:
                response = requests.get(
                    f"{url}/api/health",
                    timeout=15,
                )

                print(
                    "💓 KETS heartbeat: "
                    f"HTTP {response.status_code}"
                )

            except Exception as exc:

                print(
                    f"💓 Heartbeat failed: {exc}"
                )

        else:

            print(
                "💓 Heartbeat waiting for "
                "RENDER_EXTERNAL_URL / KETS_PUBLIC_URL"
            )

        time.sleep(
            HEARTBEAT_SECONDS
        )


# ============================================================
# WEB API
#
# KETS exposes the same main endpoints as the source so the
# frontend does not need to know where the original bot lives.
# ============================================================

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#07101b">
<title>KETS Early Entry Signals</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#07101b;color:#eef4ff;font-family:Arial,sans-serif}
.app{max-width:1100px;margin:auto;padding:18px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px}
h1{font-size:25px;margin:0 0 5px}.sub{color:#91a0b8}
.card{background:#0d1826;border:1px solid #203047;border-radius:14px;padding:15px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
.label{font-size:12px;color:#91a0b8;text-transform:uppercase}
.value{font-size:19px;font-weight:700;margin-top:6px}
.status{display:flex;align-items:center;gap:8px}.dot{width:10px;height:10px;border-radius:50%;background:#f0ad4e;display:inline-block}
.dot.ok{background:#22c878}.dot.bad{background:#ff5964}
.row{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.market,.signal{background:#101d2d;border:1px solid #263a54;border-radius:10px;padding:12px;margin-top:10px}
.buy{border-left:5px solid #22c878}.sell{border-left:5px solid #ff5964}
.small{font-size:12px;color:#91a0b8;margin-top:5px}
.muted{color:#91a0b8}
.error{color:#ff8d98}
</style>
</head>
<body>
<main class="app">
<header>
<div><h1>🤖 KETS Early Entry Signals</h1><div class="sub">Original trading bot → KETS dashboard</div></div>
<div class="status"><span id="dot" class="dot"></span><b id="top">Connecting...</b></div>
</header>

<section class="grid">
<div class="card"><div class="label">System</div><div id="system" class="value">Connecting...</div></div>
<div class="card"><div class="label">Source</div><div id="source" class="value">Checking...</div></div>
<div class="card"><div class="label">Last Poll</div><div id="lastpoll" class="value">—</div></div>
<div class="card"><div class="label">Signals</div><div id="count" class="value">0</div></div>
</section>

<section class="card">
<div class="row"><b>Live Backend Market Data</b><span id="time" class="small">—</span></div>
<div id="markets" class="muted">Waiting for market data...</div>
</section>

<section class="card">
<div class="row"><b>Latest Signals</b><span class="small">06:00–18:00 EAT scanning window</span></div>
<div id="signals" class="muted">Waiting for signals...</div>
</section>

<section class="card">
<b>Connection diagnostics</b>
<div id="diag" class="small">Checking KETS API...</div>
</section>
</main>

<script>
const $=id=>document.getElementById(id);
function esc(v){return String(v??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]))}
async function getJSON(path){
  const c=new AbortController(); const t=setTimeout(()=>c.abort(),8000);
  try{
    const r=await fetch(path,{cache:"no-store",signal:c.signal});
    if(!r.ok) throw new Error("HTTP "+r.status);
    return await r.json();
  }finally{clearTimeout(t)}
}
async function refresh(){
  try{
    const s=await getJSON("/api/status");
    $("system").textContent=s.status||"online";
    $("source").textContent=s.source_connected?"CONNECTED":"WAITING";
    $("lastpoll").textContent=s.last_poll||"—";
    $("count").textContent=s.signal_count??0;
    $("time").textContent=s.time_eat||"—";
    $("top").textContent=s.source_connected?"Original bot connected":"KETS online — source unavailable";
    $("dot").className="dot "+(s.source_connected?"ok":"bad");
    $("diag").textContent=s.source_connected
      ?"KETS API is working and the original bot is responding."
      :"KETS API is working, but the original bot has not responded yet.";
    if(s.source_last_error) $("diag").textContent+=" Last error: "+s.source_last_error;

    const m=await getJSON("/api/market");
    const entries=Object.values(m.markets||{});
    $("markets").innerHTML=entries.length?entries.map(x=>`
      <div class="market">
        <b>${esc(x.asset||"Market")} — ${esc(x.symbol||"")}</b>
        <div class="value">${x.price==null?"NO DATA":"$"+Number(x.price).toLocaleString()}</div>
        <div class="small">${esc(x.status||"LIVE")} • Signal: ${esc(x.signal||"NONE")} • Score: ${esc(x.score??"—")} • Updated: ${esc(x.updated_at||"—")}</div>
      </div>`).join(""):"Waiting for market data...";

    const q=await getJSON("/api/signals");
    const sig=q.signals||[];
    $("signals").innerHTML=sig.length?sig.slice(0,20).map(x=>`
      <div class="signal ${String(x.direction).toUpperCase()==="BUY"?"buy":"sell"}">
        <b>${String(x.direction).toUpperCase()==="BUY"?"🟢 BUY":"🔴 SELL"} — ${esc(x.asset||x.symbol||"Market")}</b>
        <div>Score: <b>${esc(x.score??"—")}%</b></div>
        <div>Entry: ${x.entry==null?"—":"$"+Number(x.entry).toLocaleString()}</div>
        <div>TP: ${x.take_profit==null?"—":"$"+Number(x.take_profit).toLocaleString()}</div>
        <div>SL: ${x.stop_loss==null?"—":"$"+Number(x.stop_loss).toLocaleString()}</div>
        <div class="small">${esc(x.timestamp||"")}</div>
      </div>`).join(""):"No signals yet.";
  }catch(e){
    $("top").textContent="KETS API ERROR";
    $("system").textContent="API error";
    $("dot").className="dot bad";
    $("diag").innerHTML='<span class="error">The page is loading, but /api/status is not responding: '+esc(e.message)+'</span>';
  }
}
refresh();
setInterval(refresh,15000);
</script>
</body>
</html>"""


@app.route("/")
def home():
    return HTML


@app.route("/api/health")
def api_health():

    with LOCK:

        return jsonify({
            "ok": True,
            "status": "online",
            "kets_engine": "source_connector",
            "source_api": SOURCE_API,
            "source_connected":
                SOURCE_STATE["connected"],
            "time_eat": iso_now(),
        })


@app.route("/api/status")
def api_status():

    with LOCK:

        source_status = (
            LAST_SOURCE_STATUS
            if isinstance(
                LAST_SOURCE_STATUS,
                dict
            )
            else {}
        )

        return jsonify({
            "status": (
                "online"
                if engine_started
                else "starting"
            ),

            "engine_started":
                engine_started,

            "mode":
                "SOURCE_BACKEND",

            "strategy_engine":
                "DISABLED",

            "source_api":
                SOURCE_API,

            "source_connected":
                SOURCE_STATE["connected"],

            "source_status":
                source_status.get(
                    "status"
                ),

            "source_last_scan":
                source_status.get(
                    "last_scan"
                ),

            "source_next_scan":
                source_status.get(
                    "next_scan"
                ),

            "last_poll":
                last_poll,

            "next_poll":
                next_poll,

            "refresh_interval_seconds":
                POLL_SECONDS,

            "signal_count":
                len(SIGNALS),

            "markets":
                list(MARKET_DATA.keys()),

            "source_last_success":
                SOURCE_STATE[
                    "last_success"
                ],

            "source_last_error":
                SOURCE_STATE[
                    "last_error"
                ],

            "time_eat":
                iso_now(),
        })


@app.route("/api/market")
def api_market():

    with LOCK:

        return jsonify({
            "markets":
                dict(MARKET_DATA),

            "source":
                SOURCE_API,

            "source_connected":
                SOURCE_STATE[
                    "connected"
                ],

            "time_eat":
                iso_now(),
        })


@app.route("/api/signals")
def api_signals():

    with LOCK:

        return jsonify({
            "signals":
                list(SIGNALS),

            "source":
                SOURCE_API,

            "source_connected":
                SOURCE_STATE[
                    "connected"
                ],

            "time_eat":
                iso_now(),
        })


@app.route("/api/source")
def api_source():

    with LOCK:

        return jsonify({
            "source_api":
                SOURCE_API,

            "connected":
                SOURCE_STATE[
                    "connected"
                ],

            "status":
                SOURCE_STATE[
                    "status"
                ],

            "last_success":
                SOURCE_STATE[
                    "last_success"
                ],

            "last_error":
                SOURCE_STATE[
                    "last_error"
                ],

            "source_status":
                LAST_SOURCE_STATUS,

            "time_eat":
                iso_now(),
        })


# ============================================================
# STARTUP
# ============================================================

def start_workers():

    global worker_started

    if worker_started:
        return

    worker_started = True

    Thread(
        target=source_worker,
        daemon=True,
        name="kets-source-worker",
    ).start()

    Thread(
        target=self_awake,
        daemon=True,
        name="kets-heartbeat",
    ).start()

    print(
        "✅ KETS background workers started"
    )


start_workers()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "8080"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
