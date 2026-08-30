import os, time, datetime, math, re, sqlite3, hashlib, base64, json, uuid
from threading import Thread, Lock
from flask import Flask, jsonify, send_from_directory, request
import requests
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

# Render Postgres is the production persistent store. SQLite remains as a
# local/dev fallback when DATABASE_URL is not configured.
try:
    import psycopg
    from psycopg.rows import dict_row
    POSTGRES_AVAILABLE = True
except Exception:
    psycopg = None
    dict_row = None
    POSTGRES_AVAILABLE = False

app = Flask(__name__)
try:
    from flask_cors import CORS
    CORS(app)
except Exception:
    pass

# ============================================================
# KETS STRATEGY ENGINE — PRODUCTION BACKEND
# 1M analysis / 2M scan / Telegram bot + channel / Web API
# ============================================================

API_LOCK = Lock()
SIGNAL_HISTORY = []
MARKET_STATE = {}
SIGNAL_HISTORY_DAYS = 7
PUBLIC_SIGNAL_DELAY_MINUTES = 30
last_signal = {}

# Private signal-source bridge. The website polls the trading bot directly so
# signal delivery does not depend on a browser request reaching /api/signals.
SOURCE_LOCK = Lock()
SOURCE_CACHE = {"signals": {}, "history": [], "last_ok": None, "last_status": None, "last_error": None}
SOURCE_POLL_SECONDS = max(5, int(os.environ.get("KETS_SOURCE_POLL_SECONDS", "10")))

def _source_config():
    url = (os.environ.get("KETS_SIGNAL_SOURCE_URL") or "").strip().rstrip("/")
    # Accept the documented website key and common bot-side names so a key
    # rename cannot silently break the bridge.
    key = (os.environ.get("KETS_SIGNAL_SOURCE_KEY") or
           os.environ.get("KETS_API_KEY") or
           os.environ.get("KETS_SIGNALS_API_KEY") or "").strip()
    return url, key

def _source_headers(key):
    return {"X-KETS-API-KEY": key, "Accept": "application/json"}

def _sync_signal_source_once():
    """Pull and normalize signals from the private trading-bot API.

    The source bot exposes /api/signals (a list), while older deployments
    may not expose /api/history. The bridge therefore uses /api/signals as
    the compatible primary feed and optionally consumes /api/history.
    """
    url, key = _source_config()
    if not url:
        return False
    if not key:
        with SOURCE_LOCK:
            SOURCE_CACHE["last_status"] = "missing_key"
            SOURCE_CACHE["last_error"] = "KETS signal source URL is set but no source API key is configured."
        print("⚠️ KETS source bridge: URL configured but API key is missing.")
        return False

    try:
        headers = _source_headers(key)
        history = []

        try:
            # Private engine-history endpoint is authenticated only with the
            # shared server key, so the website can import signals that were
            # generated before the website database received them.
            rh = requests.get(url + "/api/engine-history", headers=headers, timeout=15)
            print(f"📥 KETS source bridge: GET /api/engine-history -> {rh.status_code}")
            if rh.status_code == 200:
                dh = rh.json()
                if isinstance(dh, dict) and isinstance(dh.get("history"), list):
                    history = [x for x in dh["history"] if isinstance(x, dict)]
            elif rh.status_code not in (401, 404):
                print(f"⚠️ KETS source bridge: engine history returned HTTP {rh.status_code}")
            if not history:
                # The deployed trading bot exposes its seven-day history via
                # authenticated GET /api/signals?limit=200. Import that feed
                # as a compatibility path for existing bot deployments.
                rh = requests.get(url + "/api/signals?limit=200", headers=headers, timeout=15)
                print(f"📥 KETS source bridge: fallback GET /api/signals?limit=200 -> {rh.status_code}")
                if rh.status_code == 200:
                    dh = rh.json()
                    if isinstance(dh, dict) and isinstance(dh.get("signals"), list):
                        history = [x for x in dh["signals"] if isinstance(x, dict)]
        except Exception as e:
            print(f"⚠️ KETS source bridge: history unavailable: {str(e)[:180]}")

        r = requests.get(url + "/api/signals", headers=headers, timeout=15)
        print(f"📥 KETS source bridge: GET /api/signals -> {r.status_code}")
        if r.status_code != 200:
            with SOURCE_LOCK:
                SOURCE_CACHE["last_status"] = r.status_code
                SOURCE_CACHE["last_error"] = f"Source returned HTTP {r.status_code}"
            return False

        data = r.json()
        raw_current = data.get("signals", []) if isinstance(data, dict) else []

        current_items = []
        if isinstance(raw_current, list):
            current_items = [x for x in raw_current if isinstance(x, dict)]
        elif isinstance(raw_current, dict):
            current_items = [x for x in raw_current.values() if isinstance(x, dict)]

        if not history:
            history = list(current_items)

        latest = {}
        for item in current_items + history:
            asset = item.get("asset", item.get("market", "UNKNOWN"))
            old_item = latest.get(asset)
            if old_item is None or str(item.get("timestamp", "")) > str(old_item.get("timestamp", "")):
                latest[asset] = item

        # Import the source history into the permanent website database.
        # _persist_signal is idempotent, so repeated bridge polls are safe.
        imported = 0
        for item in history:
            try:
                normalized = _normalize_received_signal(item)
                _persist_signal(normalized)
                imported += 1
            except Exception:
                continue

        with SOURCE_LOCK:
            SOURCE_CACHE["signals"] = latest
            SOURCE_CACHE["history"] = history[-500:]
            SOURCE_CACHE["last_ok"] = get_eat_time().isoformat()
            SOURCE_CACHE["last_status"] = 200
            SOURCE_CACHE["last_error"] = None

        print(f"💾 KETS source bridge: imported {imported} history item(s) into permanent storage")

        print(f"✅ KETS source bridge: received {len(latest)} current signal(s), {len(history)} history item(s)")
        return True

    except Exception as e:
        with SOURCE_LOCK:
            SOURCE_CACHE["last_status"] = "error"
            SOURCE_CACHE["last_error"] = str(e)[:300]
        print(f"⚠️ KETS source bridge error: {str(e)[:300]}")
        return False

def run_signal_source_bridge():
    print("📡 KETS source bridge starting")
    url, key = _source_config()
    if url:
        print(f"🔗 Signal source configured: {url}")
    else:
        print("⚠️ Signal source URL not configured; website-local signals only")
    while True:
        try:
            if _source_config()[0]:
                _sync_signal_source_once()
        except Exception as e:
            print(f"⚠️ KETS source bridge loop error: {str(e)[:300]}")
        time.sleep(SOURCE_POLL_SECONDS)



def get_eat_time():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)


def trading_hours_open():
    t = get_eat_time().time()
    return datetime.time(6, 0) <= t < datetime.time(18, 0)


def get_markets():
    if get_eat_time().weekday() >= 5:
        return {"BTC": "BTC/USD"}
    return {"BTC": "BTC/USD", "GOLD": "XAU/USD"}


def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def keep_web_server_alive():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


def _source_snapshot():
    with SOURCE_LOCK:
        return dict(SOURCE_CACHE)

def signal_window():
    now = get_eat_time()
    start = now.replace(hour=6, minute=0, second=0, microsecond=0)
    stop = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if now < start:
        return {"active": False, "seconds_to_start": int((start-now).total_seconds()), "seconds_to_stop": 0}
    if now >= stop:
        tomorrow = start + datetime.timedelta(days=1)
        return {"active": False, "seconds_to_start": int((tomorrow-now).total_seconds()), "seconds_to_stop": 0}
    return {"active": True, "seconds_to_start": 0, "seconds_to_stop": int((stop-now).total_seconds())}


# ------------------------- WEB API ---------------------------
@app.route("/")
def home():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.route("/app.js")
def app_js():
    """Serve the normal frontend plus the authoritative 30-minute release timer."""
    base = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "app.js")
    with open(path, "r", encoding="utf-8") as fh:
        js = fh.read()
    js += '\n/* KETS 30-minute public-release timer (server authoritative) */\n(function(){\n  let ketsTimerReleaseMs=null, ketsTimerOffsetMs=0, ketsTimerPaid=false;\n  function now(){return Date.now()+ketsTimerOffsetMs;}\n  function fmt(sec){sec=Math.max(0,Math.ceil(Number(sec)||0));return String(Math.floor(sec/60)).padStart(2,"0")+":"+String(sec%60).padStart(2,"0");}\n  function ensure(){\n    let el=document.getElementById("kets30mTimer"); if(el)return el;\n    const grid=document.getElementById("signalGrid"); if(!grid)return null;\n    el=document.createElement("div"); el.id="kets30mTimer";\n    el.style.cssText="margin:0 16px 16px;padding:16px 18px;border:1px solid rgba(130,110,255,.25);border-radius:18px;background:linear-gradient(135deg,rgba(15,27,49,.98),rgba(10,19,34,.98));display:flex;align-items:center;justify-content:space-between;gap:16px;box-shadow:0 8px 30px rgba(0,0,0,.16);";\n    el.innerHTML=\'<div><div style="font-size:11px;letter-spacing:.12em;text-transform:uppercase;opacity:.7">FREE SIGNAL RELEASE</div><div id="kets30mLabel" style="margin-top:5px;font-weight:600">Waiting for the next signal</div></div><strong id="kets30mValue" style="font-size:28px;letter-spacing:.04em;white-space:nowrap">--:--</strong>\';\n    grid.parentNode.insertBefore(el,grid); return el;\n  }\n  function draw(){\n    const el=ensure(); if(!el)return;\n    const label=document.getElementById("kets30mLabel"), value=document.getElementById("kets30mValue");\n    if(ketsTimerPaid){el.style.display="none";return;} el.style.display="flex";\n    if(ketsTimerReleaseMs && ketsTimerReleaseMs>now()){\n      label.textContent="Next signal becomes visible to free users in"; value.textContent=fmt((ketsTimerReleaseMs-now())/1000);\n    }else if(ketsTimerReleaseMs){label.textContent="Signal release time reached — refreshing";value.textContent="00:00";}\n    else{label.textContent="No unreleased signal is waiting right now";value.textContent="--:--";}\n  }\n  async function sync(){\n    try{\n      const token=sessionStorage.getItem("kets_user_token"); if(!token)return;\n      const r=await fetch(window.location.origin+"/api/signals",{headers:{Accept:"application/json",Authorization:"Bearer "+token}}); const d=await r.json(); if(!r.ok)return;\n      ketsTimerPaid=d.mode==="live"||Number(d.delay_minutes||0)===0;\n      const server=Date.parse(d.time_eat||""); if(Number.isFinite(server))ketsTimerOffsetMs=server-Date.now();\n      const rel=Date.parse(d.next_public_release_at||""); ketsTimerReleaseMs=Number.isFinite(rel)?rel:null; draw();\n      if(ketsTimerReleaseMs&&ketsTimerReleaseMs<=now()+1000)setTimeout(sync,1500);\n    }catch(e){}\n  }\n  setInterval(draw,1000); setInterval(sync,10000); sync();\n})();\n'
    return app.response_class(js, mimetype="application/javascript")


@app.route("/styles.css")
def styles_css():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "styles.css")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "manifest.json")


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "service-worker.js")


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "status": "online", "time_eat": get_eat_time().isoformat()})


@app.route("/api/status")
def api_status():
    with API_LOCK:
        return jsonify({
            "status": "online",
            "engine_running": bool(globals().get("engine_started", False)) or bool(os.environ.get("KETS_SIGNAL_SOURCE_URL")),
            "data_provider_configured": bool(os.environ.get("TWELVE_DATA_API_KEY")),
            "refresh_interval_seconds": 120,
            "history_days": SIGNAL_HISTORY_DAYS,
            "trading_hours_eat": "06:00-18:00",
            "signal_window": signal_window(),
            "next_broadcast_seconds": max(0, int((datetime.datetime.fromisoformat(globals().get("next_scan", get_eat_time().isoformat())) - get_eat_time()).total_seconds())) if globals().get("next_scan") else 0,
            "server_time": get_eat_time().isoformat(),
            "last_scan": globals().get("last_scan"),
            "next_scan": globals().get("next_scan"),
            "markets": list(MARKET_STATE.keys()),
            "signal_count": len(SIGNAL_HISTORY),
        })


@app.route("/api/market")
def api_market():
    with API_LOCK:
        markets = {}
        for asset, item in MARKET_STATE.items():
            safe = dict(item)
            # Keep raw market price public, but hide strategy-derived fields when locked.
            if not web_access_paid():
                safe.pop("signal", None)
                safe.pop("score", None)
            markets[asset] = safe
        return jsonify({"markets": markets, "time_eat": get_eat_time().isoformat()})


def _receiver_key():
    """Secret accepted by the bot -> website POST receiver.

    Keep this server-side only.  By default it uses the same shared secret
    already documented for the KETS bridge, while allowing a dedicated
    receiver key when desired.
    """
    return (os.environ.get("KETS_SIGNAL_RECEIVER_KEY") or
            os.environ.get("KETS_SIGNAL_SOURCE_KEY") or
            os.environ.get("KETS_API_KEY") or
            os.environ.get("KETS_SIGNALS_API_KEY") or "").strip()


def _receiver_authorized():
    expected = _receiver_key()
    supplied = request.headers.get("X-KETS-API-KEY", "").strip()
    return bool(expected and supplied) and secrets.compare_digest(supplied, expected)


def _normalize_received_signal(payload):
    """Normalize the trading bot's API payload to the website signal shape."""
    item = dict(payload)
    asset = str(item.get("asset", item.get("market", ""))).strip().upper()
    direction = str(item.get("direction", "")).strip().upper()

    if not asset:
        raise ValueError("asset is required")
    if direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")

    required = ("id", "score", "market_price", "take_profit", "stop_loss")
    missing = [key for key in required if item.get(key) is None]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))

    try:
        score = float(item["score"])
        market_price = float(item["market_price"])
        take_profit = float(item["take_profit"])
        stop_loss = float(item["stop_loss"])
    except (TypeError, ValueError):
        raise ValueError("score, market_price, take_profit and stop_loss must be numeric")

    if not all(math.isfinite(x) for x in (score, market_price, take_profit, stop_loss)):
        raise ValueError("numeric signal fields must be finite")

    timestamp = item.get("timestamp") or get_eat_time().isoformat()
    timestamp_utc = item.get("timestamp_utc")

    # Preserve the bot's richer fields and add aliases expected by the
    # existing website frontend/history renderer.
    item.update({
        "id": str(item["id"]),
        "asset": asset,
        "market": asset,
        "direction": direction,
        "score": score,
        "strength": score,
        "market_price": market_price,
        "entry": market_price,
        "price": market_price,
        "current_price": market_price,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "expected_price_move": item.get("expected_price_move", item.get("price_move")),
        "expected_price_move_percent": item.get("expected_price_move_percent", item.get("price_move_pct")),
        "expected_move": item.get("expected_move", item.get("price_move")),
        "expected_move_pct": item.get("expected_move_pct", item.get("price_move_pct")),
        "estimated_duration": item.get("estimated_duration", item.get("duration_text")),
        "timestamp": timestamp,
        "status": item.get("status", "ACTIVE"),
        "received_at": get_eat_time().isoformat(),
    })
    if timestamp_utc:
        item["timestamp_utc"] = timestamp_utc
    return item


def _store_received_signal(item):
    """Idempotently store an authenticated pushed signal for website use."""
    now = get_eat_time()
    cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
    with API_LOCK:
        # Retain only recent entries first.
        kept = []
        for existing in SIGNAL_HISTORY:
            try:
                if datetime.datetime.fromisoformat(existing["timestamp"]) >= cutoff:
                    kept.append(existing)
            except Exception:
                continue

        duplicate = any(existing.get("id") == item.get("id") for existing in kept)
        if not duplicate:
            kept.append(item)
        SIGNAL_HISTORY[:] = kept[-500:]

    # Make the pushed signal immediately visible to the website feed even
    # when the optional pull bridge is disabled or unavailable.
    with SOURCE_LOCK:
        latest = dict(SOURCE_CACHE.get("signals", {}))
        asset = item.get("asset", item.get("market", "UNKNOWN"))
        previous = latest.get(asset)
        if previous is None or str(item.get("timestamp", "")) >= str(previous.get("timestamp", "")):
            latest[asset] = item
        source_history = list(SOURCE_CACHE.get("history", []))
        if not any(x.get("id") == item.get("id") for x in source_history):
            source_history.append(item)
        SOURCE_CACHE["signals"] = latest
        SOURCE_CACHE["history"] = source_history[-500:]
        SOURCE_CACHE["last_ok"] = now.isoformat()
        SOURCE_CACHE["last_status"] = 200
        SOURCE_CACHE["last_error"] = None

    # Permanent database storage is independent of the in-memory API cache.
    # This survives Render restarts/deploys when DATABASE_URL points to the
    # KETS Render Postgres database.
    _persist_signal(item)
    return duplicate


@app.route("/api/signals", methods=["POST"])
def api_receive_signal():
    """Receive an authenticated signal pushed directly from the trading bot."""
    if not _receiver_authorized():
        print("❌ KETS website rejected signal: unauthorized API key")
        return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON payload"}), 400

    try:
        item = _normalize_received_signal(payload)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    duplicate = _store_received_signal(item)
    print(
        f"📥 WEBSITE SIGNAL RECEIVED: {item['asset']} "
        f"{item['direction']} {item['id']}"
    )
    print(
        f"✅ KETS website received signal: {item['id']} "
        f"({'duplicate' if duplicate else 'stored'})"
    )
    return jsonify({
        "status": "success",
        "message": "Signal received",
        "id": item["id"],
        "duplicate": duplicate,
        "asset": item["asset"],
        "direction": item["direction"],
    }), 200 if duplicate else 201


def _next_public_release_at(history, now):
    """Return earliest hidden signal release time without exposing its payload."""
    cutoff=now-datetime.timedelta(minutes=PUBLIC_SIGNAL_DELAY_MINUTES)
    releases=[]
    for x in history or []:
        try:
            dt=datetime.datetime.fromisoformat(str(x.get("timestamp","")))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=now.tzinfo)
        except Exception: continue
        if dt>cutoff and dt<=now and dt>=now-datetime.timedelta(days=SIGNAL_HISTORY_DAYS):
            releases.append(dt+datetime.timedelta(minutes=PUBLIC_SIGNAL_DELAY_MINUTES))
    return min(releases).isoformat() if releases else None


@app.route("/api/signals")
def api_signals():
    # Paid users receive the current signal feed. Unpaid users receive the
    # same signals only after a strict 30-minute server-side delay.
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in required."}), 401

    paid = bool(_user_access(user["id"])) or os.environ.get("KETS_ACCESS", "").lower() == "paid"
    source_url = os.environ.get("KETS_SIGNAL_SOURCE_URL", "").strip().rstrip("/")
    source_key = _source_config()[1]
    now = get_eat_time()
    cutoff = now - datetime.timedelta(minutes=PUBLIC_SIGNAL_DELAY_MINUTES)

    if paid:
        if source_url and source_key:
            snap = _source_snapshot()
            if snap.get("last_ok"):
                return jsonify({
                    "signals": snap.get("signals", {}),
                    "time_eat": now.isoformat(),
                    "source": "private KETS strategy engine",
                    "mode": "live",
                    "source_last_ok": snap.get("last_ok"),
                })
            # First request can be served immediately if the bridge has not
            # completed yet; force one synchronous attempt rather than waiting.
            if _sync_signal_source_once():
                snap = _source_snapshot()
                return jsonify({"signals": snap.get("signals", {}), "time_eat": now.isoformat(), "source": "private KETS strategy engine", "mode": "live", "source_last_ok": snap.get("last_ok")})
            return jsonify({"error": "Unable to reach the private signal engine.", "source_status": snap.get("last_status")}), 502

        history_snapshot = _load_persistent_signals()
        if not history_snapshot:
            with API_LOCK:
                history_snapshot = list(SIGNAL_HISTORY)
        latest = {}
        for x in history_snapshot:
            latest[x.get("asset", x.get("market", "UNKNOWN"))] = x
        return jsonify({
            "signals": latest,
            "time_eat": now.isoformat(),
            "source": "website-local engine",
            "mode": "live",
        })

    # Unpaid users get the newest signal for each market that is at least
    # 30 minutes old. The filter is performed here on the server so it cannot
    # be bypassed by changing browser JavaScript.
    if source_url and source_key:
        snap = _source_snapshot()
        history = list(snap.get("history", [])) if snap.get("last_ok") else _fetch_signal_history_from_source(source_url, source_key)
    else:
        history = None
    if history is None:
        history = _load_persistent_signals()
        if not history:
            with API_LOCK:
                history = list(SIGNAL_HISTORY)

    latest = {}
    for x in history:
        try:
            dt = datetime.datetime.fromisoformat(x["timestamp"])
        except Exception:
            continue
        if dt <= cutoff and dt >= now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS):
            asset = x.get("asset", x.get("market", "UNKNOWN"))
            previous = latest.get(asset)
            if previous is None or x.get("timestamp", "") > previous.get("timestamp", ""):
                latest[asset] = x

    return jsonify({
        "signals": latest,
        "time_eat": now.isoformat(),
        "source": "private KETS strategy engine" if source_url else "website-local engine",
        "mode": "delayed",
        "delay_minutes": PUBLIC_SIGNAL_DELAY_MINUTES,
        "available_after": cutoff.isoformat(),
        "next_public_release_at": _next_public_release_at(history, now),
    })


def _fetch_signal_history_from_source(source_url, source_key):
    if not source_url:
        return None
    try:
        headers = {"Accept": "application/json"}
        if source_key:
            headers["X-KETS-API-KEY"] = source_key
        r = requests.get(source_url.rstrip("/") + "/api/history", headers=headers, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("history", [])
    except Exception:
        return None


@app.route("/api/engine-history")
def api_engine_history():
    """Private source-history endpoint for the KETS website bridge.

    It deliberately uses the server-to-server API key rather than a user
    session, allowing the website to import the bot's existing in-memory
    seven-day history after deployment.
    """
    if not _receiver_authorized():
        return jsonify({"error": "Unauthorized"}), 401
    with API_LOCK:
        history = list(SIGNAL_HISTORY)[-500:]
    return jsonify({
        "history": history,
        "count": len(history),
        "time_eat": get_eat_time().isoformat(),
    })


@app.route("/api/history")
def api_history():
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in required."}), 401

    paid = bool(_user_access(user["id"])) or os.environ.get("KETS_ACCESS", "").lower() == "paid"
    now = get_eat_time()
    cutoff_7d = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
    cutoff_delay = now - datetime.timedelta(minutes=PUBLIC_SIGNAL_DELAY_MINUTES)

    source_url = os.environ.get("KETS_SIGNAL_SOURCE_URL", "").strip().rstrip("/")
    source_key = _source_config()[1]
    if source_url and source_key:
        snap = _source_snapshot()
        history = list(snap.get("history", [])) if snap.get("last_ok") else _fetch_signal_history_from_source(source_url, source_key)
    else:
        history = None
    if history is None:
        history = _load_persistent_signals()
        if not history:
            with API_LOCK:
                history = list(SIGNAL_HISTORY)

    items = []
    for x in history:
        try:
            dt = datetime.datetime.fromisoformat(x["timestamp"])
        except Exception:
            continue
        if dt < cutoff_7d:
            continue
        if not paid and dt > cutoff_delay:
            continue
        items.append(x)

    return jsonify({
        "history": items[-500:],
        "mode": "live" if paid else "delayed",
        "delay_minutes": 0 if paid else PUBLIC_SIGNAL_DELAY_MINUTES,
        "time_eat": now.isoformat(),
    })


@app.route("/api/source-status")
def api_source_status():
    # Operational diagnostics; never expose the secret key.
    url, key = _source_config()
    snap = _source_snapshot()
    return jsonify({
        "configured": bool(url and key),
        "source_url": url,
        "key_configured": bool(key),
        "last_ok": snap.get("last_ok"),
        "last_status": snap.get("last_status"),
        "last_error": snap.get("last_error"),
        "signal_count": len(snap.get("signals", {})),
        "history_count": len(snap.get("history", [])),
        "poll_seconds": SOURCE_POLL_SECONDS,
    })


# ============================================================
# KETS ACCOUNTS / PROFILES / SUBSCRIPTIONS
# ============================================================
# Production: Render Postgres via DATABASE_URL.
# Local/dev fallback: SQLite kets.db.
# Postgres is preferred automatically whenever DATABASE_URL is present.
DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
DB_PATH = os.environ.get("KETS_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "kets.db"))
DB_LOCK = Lock()
COUNTRY_REQUIRED = True

def using_postgres():
    return bool(DATABASE_URL)

def db_conn():
    if using_postgres():
        if not POSTGRES_AVAILABLE:
            raise RuntimeError("DATABASE_URL is configured but psycopg is not installed.")
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=15)
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _db_sql(sql):
    # Existing KETS queries use SQLite-style ? placeholders. Translate them
    # centrally for PostgreSQL so the application logic stays unchanged.
    if using_postgres():
        sql = sql.replace(" COLLATE NOCASE", "")
        sql = sql.replace("?", "%s")
    return sql

def db_execute(conn, sql, params=()):
    return conn.execute(_db_sql(sql), params)

def init_db():
    with DB_LOCK:
        conn = db_conn()
        if using_postgres():
            statements = [
                """CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT,
                    name TEXT NOT NULL DEFAULT '',
                    country_name TEXT NOT NULL DEFAULT '',
                    country_code TEXT NOT NULL DEFAULT '',
                    profile_picture TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    tx_ref TEXT NOT NULL UNIQUE,
                    tracking_id TEXT,
                    plan TEXT NOT NULL,
                    amount DOUBLE PRECISION NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'UGX',
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    network TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)",
                "CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status)",
                """CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    asset TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    score DOUBLE PRECISION NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""",
                "CREATE INDEX IF NOT EXISTS idx_signals_asset_time ON signals(asset, timestamp)",
                "CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(timestamp)",
            ]
            for statement in statements:
                conn.execute(statement)
        else:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT,
                name TEXT NOT NULL DEFAULT '',
                country_name TEXT NOT NULL DEFAULT '',
                country_code TEXT NOT NULL DEFAULT '',
                profile_picture TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                tx_ref TEXT NOT NULL UNIQUE,
                tracking_id TEXT,
                plan TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'UGX',
                status TEXT NOT NULL DEFAULT 'PENDING',
                network TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                direction TEXT NOT NULL,
                score REAL NOT NULL,
                timestamp TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_asset_time ON signals(asset, timestamp);
            CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(timestamp);
            """)
        conn.commit()
        conn.close()

init_db()

def _now_iso():
    return get_eat_time().isoformat()

def _hash_password(password):
    salt = os.urandom(16)
    rounds = 210000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return f"pbkdf2_sha256${rounds}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"

def _check_password(password, stored):
    try:
        scheme, rounds, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt_b64), int(rounds))
        return secrets.compare_digest(base64.urlsafe_b64encode(digest).decode(), digest_b64)
    except Exception:
        return False

def _auth_serializer():
    return URLSafeTimedSerializer(_payment_secret(), salt="kets-user-v2")

def _user_token(user_id):
    return _auth_serializer().dumps({"user_id": user_id})

def _current_user():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip() or request.cookies.get("kets_user", "")
    if not token:
        return None
    try:
        payload = _auth_serializer().loads(token, max_age=30 * 24 * 3600)
        with DB_LOCK:
            conn = db_conn()
            row = db_execute(conn, "SELECT * FROM users WHERE id=?", (payload["user_id"],)).fetchone()
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None

def _safe_user(row):
    if not row:
        return None
    d = dict(row)
    d.pop("password_hash", None)
    return d

def _valid_email(email):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email or ""))

def _normalize_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    if digits.startswith("256"):
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+256" + digits[1:]
    return phone.strip()

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    name = str(body.get("name", "")).strip()
    country_name = str(body.get("country_name", "")).strip()
    country_code = str(body.get("country_code", "")).strip().upper()
    if country_code == "UG":
        country_name = "Uganda"
    elif country_code == "OTHER":
        country_name = "Other"
    else:
        return jsonify({"error": "Select Uganda or Other."}), 400
    if not _valid_email(email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters."}), 400
    if COUNTRY_REQUIRED and (not country_name or not country_code):
        return jsonify({"error": "Select your country and country code."}), 400
    now = _now_iso()
    user_id = str(uuid.uuid4())
    with DB_LOCK:
        conn = db_conn()
        try:
            db_execute(conn, 
                "INSERT INTO users(id,email,password_hash,name,country_name,country_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (user_id,email,_hash_password(password),name,country_name,country_code,now,now)
            )
            conn.commit()
        except Exception as exc:
            if not isinstance(exc, sqlite3.IntegrityError) and not (POSTGRES_AVAILABLE and isinstance(exc, psycopg.IntegrityError)):
                raise
            conn.close()
            return jsonify({"error": "That email already has an account. Please sign in."}), 409
        conn.close()
    return jsonify({"ok": True, "token": _user_token(user_id), "user": _safe_user({"id":user_id,"email":email,"name":name,"country_name":country_name,"country_code":country_code,"profile_picture":"","created_at":now,"updated_at":now})})

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    with DB_LOCK:
        conn = db_conn()
        row = db_execute(conn, "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        conn.close()
    if not row or not row["password_hash"] or not _check_password(password, row["password_hash"]):
        return jsonify({"error": "Email or password is incorrect."}), 401
    return jsonify({"ok": True, "token": _user_token(row["id"]), "user": _safe_user(row)})

@app.route("/api/auth/me")
def api_auth_me():
    user = _current_user()
    if not user:
        return jsonify({"authenticated": False}), 401
    return jsonify({"authenticated": True, "user": _safe_user(user), "access": _user_access(user["id"])})

@app.route("/api/auth/profile", methods=["PUT"])
def api_profile():
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in required."}), 401
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", user["name"])).strip()[:100]
    country_name = user["country_name"]
    country_code = user["country_code"]
    profile_picture = ""
    if COUNTRY_REQUIRED and (not country_name or not country_code):
        return jsonify({"error": "Country selection is required."}), 400
    with DB_LOCK:
        conn = db_conn()
        db_execute(conn, "UPDATE users SET name=?,country_name=?,country_code=?,profile_picture=?,updated_at=? WHERE id=?",
                     (name,country_name,country_code,profile_picture,_now_iso(),user["id"]))
        conn.commit()
        row = db_execute(conn, "SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        conn.close()
    return jsonify({"ok": True, "user": _safe_user(row)})

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie("kets_user")
    resp.delete_cookie("kets_access")
    return resp

@app.route("/api/community")
def api_community():
    now = get_eat_time().isoformat()
    counts = {k:0 for k in PAYMENT_PLANS}
    with DB_LOCK:
        conn = db_conn()
        rows = db_execute(conn, "SELECT plan, COUNT(*) n FROM payments WHERE status='COMPLETED' AND updated_at IS NOT NULL GROUP BY plan").fetchall()
        conn.close()
    # A payment is active when its expiry is in the future.
    for row in rows:
        counts[row["plan"]] = int(row["n"])
    active = {}
    with DB_LOCK:
        conn = db_conn()
        rows = db_execute(conn, "SELECT plan, user_id, created_at, updated_at FROM payments WHERE status='COMPLETED'").fetchall()
        conn.close()
    for row in rows:
        exp = _subscription_expiry(row["plan"], row["updated_at"])
        if exp > get_eat_time():
            active[row["plan"]] = active.get(row["plan"],0)+1
    return jsonify({"counts": active, "labels": {k:v["name"] for k,v in PAYMENT_PLANS.items()}, "currency":"UGX", "as_of":now})

@app.route("/api/payments/history")
def api_payment_history():
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in required."}), 401
    with DB_LOCK:
        conn = db_conn()
        rows = db_execute(conn, "SELECT tx_ref,plan,amount,currency,status,network,created_at,updated_at,tracking_id FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(user["id"],)).fetchall()
        conn.close()
    return jsonify({"payments":[dict(r) for r in rows], "currency":"UGX"})

@app.route("/developer")
@app.route("/developer/dashboard")
def developer_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "developer.html")

@app.route("/api/developer/login", methods=["POST"])
def developer_login():
    body = request.get_json(silent=True) or {}
    u = str(body.get("username",""))
    p = str(body.get("password",""))
    eu = os.environ.get("KETS_DEVELOPER_USERNAME","").strip()
    ep = os.environ.get("KETS_DEVELOPER_PASSWORD","")
    if not eu or not ep or not secrets.compare_digest(u,eu) or not secrets.compare_digest(p,ep):
        return jsonify({"error":"Invalid developer credentials."}),401
    token = _auth_serializer().dumps({"developer":True,"nonce":secrets.token_urlsafe(12)})
    return jsonify({"ok":True,"token":token})

def _developer_ok():
    token=request.headers.get("Authorization","").removeprefix("Bearer ").strip()
    if not token: return False
    try:
        return bool(_auth_serializer().loads(token,max_age=12*3600).get("developer"))
    except Exception:
        return False

@app.route("/api/developer/signals")
def developer_signals():
    """Private owner-only feed: full current signals, regardless of subscriber status."""
    if not _developer_ok():
        return jsonify({"error":"Developer authentication required."}),401

    source_url = os.environ.get("KETS_SIGNAL_SOURCE_URL", "").strip().rstrip("/")
    source_key = _source_config()[1]
    now = get_eat_time()

    # Prefer the private strategy source when configured so the owner can see
    # the same full payload used by the Telegram bot, without the public delay.
    if source_url and source_key:
        try:
            r = requests.get(
                source_url + "/api/signals",
                headers={"X-KETS-API-KEY": source_key, "Accept": "application/json"},
                timeout=5,
            )
            if r.status_code == 200:
                data = r.json()
                return jsonify({
                    "signals": data.get("signals", {}),
                    "history": data.get("history", []),
                    "mode": "owner-live",
                    "time_eat": now.isoformat(),
                    "source": "private KETS strategy engine",
                })
        except Exception:
            pass

    with API_LOCK:
        recent = list(SIGNAL_HISTORY)[-100:]
        latest = {}
        for item in recent:
            asset = item.get("asset", item.get("market", "UNKNOWN"))
            latest[asset] = dict(item)
    return jsonify({
        "signals": latest,
        "history": recent,
        "mode": "owner-live",
        "time_eat": now.isoformat(),
        "source": "website-local engine",
    })


@app.route("/api/developer/data")
def developer_data():
    if not _developer_ok():
        return jsonify({"error":"Developer authentication required."}),401
    with API_LOCK:
        signals=[dict(x) for x in SIGNAL_HISTORY[-200:]]
        markets={k:dict(v) for k,v in MARKET_STATE.items()}
    users=[]
    payments=[]
    database_error=""
    try:
        with DB_LOCK:
            conn=db_conn()
            users=db_execute(conn, "SELECT id,email,name,country_name,country_code,created_at FROM users ORDER BY created_at DESC LIMIT 500").fetchall()
            payments=db_execute(conn, "SELECT * FROM payments ORDER BY created_at DESC LIMIT 500").fetchall()
            conn.close()
    except Exception as exc:
        database_error="Database temporarily unavailable."
        print(f"⚠️ Developer dashboard database read failed: {exc}")
    return jsonify({"signals":signals,"markets":markets,"users":[dict(x) for x in users],"payments":[dict(x) for x in payments],"database_error":database_error,"community":None})


PAYMENT_PLANS = {
    "30_min": {"name": "30 Minutes", "ugx": 500, "seconds": 30*60},
    "1_hour": {"name": "1 Hour", "ugx": 1000, "seconds": 60*60},
    "4_hour": {"name": "4 Hours", "ugx": 3000, "seconds": 4*60*60},
    "1_day": {"name": "1 Day", "ugx": 5000, "usd": 1, "seconds": 24*60*60},
    "1_week": {"name": "1 Week", "ugx": 30000, "usd": 5, "seconds": 7*24*60*60},
    "1_month": {"name": "1 Month", "ugx": 50000, "usd": 15, "seconds": 30*24*60*60},
    "1_year": {"name": "1 Year", "ugx": 1000000, "seconds": 365*24*60*60},
}
def _subscription_expiry(plan_id, start_iso):
    try:
        return datetime.datetime.fromisoformat(start_iso) + datetime.timedelta(seconds=PAYMENT_PLANS[plan_id]["seconds"])
    except Exception:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

def _user_access(user_id):
    with DB_LOCK:
        conn=db_conn()
        rows=db_execute(conn, "SELECT * FROM payments WHERE user_id=? AND status='COMPLETED' ORDER BY updated_at DESC",(user_id,)).fetchall()
        conn.close()
    active=[]
    for row in rows:
        exp=_subscription_expiry(row["plan"], row["updated_at"])
        if exp > get_eat_time():
            active.append({"plan":row["plan"],"expires":exp.isoformat(),"tx_ref":row["tx_ref"]})
    active.sort(key=lambda x:x["expires"], reverse=True)
    return active[0] if active else None


# ============================================================
# PESAPAL API 3.0 PAYMENT LAYER
# ============================================================
# Render URL can be overridden with KETS_PUBLIC_URL if you later
# attach a custom domain. Default is the current KETS Render service.
PESAPAL_ENVIRONMENT = os.environ.get("PESAPAL_ENVIRONMENT", "live").lower().strip()
KETS_PUBLIC_URL = os.environ.get("KETS_PUBLIC_URL", "https://kets.onrender.com").rstrip("/")
PESAPAL_IPN_URL = f"{KETS_PUBLIC_URL}/api/payments/ipn"
PESAPAL_CALLBACK_URL = f"{KETS_PUBLIC_URL}/api/payments/callback"
PESAPAL_CANCEL_URL = f"{KETS_PUBLIC_URL}/api/payments/cancel"

PESAPAL_BASE_URL = (
    "https://cybqa.pesapal.com/pesapalv3/api"
    if PESAPAL_ENVIRONMENT in {"sandbox", "demo", "test"}
    else "https://pay.pesapal.com/v3/api"
)

PAYMENT_ORDERS = {}
PAYMENT_ORDERS_LOCK = Lock()


def _payment_secret():
    return os.environ.get("KETS_SESSION_SECRET") or os.environ.get("PESAPAL_CONSUMER_SECRET") or "CHANGE_ME"


def _serializer():
    return URLSafeTimedSerializer(_payment_secret(), salt="kets-access-v1")


def _access_token(plan_id, tx_ref, user_id):
    plan = PAYMENT_PLANS[plan_id]
    expires = get_eat_time() + datetime.timedelta(seconds=plan["seconds"])
    payload = {"plan": plan_id, "tx_ref": tx_ref, "user_id": user_id, "expires": expires.isoformat()}
    return _serializer().dumps(payload)


def _token_access():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get("kets_access", "")
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=366 * 24 * 3600)
        expiry = datetime.datetime.fromisoformat(data["expires"])
        if expiry > get_eat_time():
            return data
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        pass
    return None


def web_access_paid():
    if os.environ.get("KETS_ACCESS", "").lower() == "paid":
        return True
    user = _current_user()
    if not user:
        return False
    return _user_access(user["id"]) is not None


def _pesapal_credentials():
    key = os.environ.get("PESAPAL_CONSUMER_KEY", "").strip()
    secret = os.environ.get("PESAPAL_CONSUMER_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("PESAPAL_CONSUMER_KEY and PESAPAL_CONSUMER_SECRET are not configured")
    return key, secret


def _pesapal_token():
    key, secret = _pesapal_credentials()
    try:
        r = requests.post(
            f"{PESAPAL_BASE_URL}/Auth/RequestToken",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"consumer_key": key, "consumer_secret": secret},
            timeout=30,
        )
        data = r.json()
    except Exception as exc:
        raise RuntimeError(f"Pesapal authentication failed: {exc}") from exc
    if r.status_code >= 400 or not data.get("token"):
        raise RuntimeError(data.get("message", "Pesapal authentication failed"))
    return data["token"]


def _pesapal_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _plan_from_ref(tx_ref):
    prefix = "KETS-"
    if not tx_ref.startswith(prefix):
        return None
    plan_id = tx_ref.split("-", 2)[1].lower()
    return plan_id if plan_id in PAYMENT_PLANS else None


def _pesapal_status(order_tracking_id):
    token = _pesapal_token()
    try:
        r = requests.get(
            f"{PESAPAL_BASE_URL}/Transactions/GetTransactionStatus",
            params={"orderTrackingId": order_tracking_id},
            headers=_pesapal_headers(token),
            timeout=30,
        )
        data = r.json()
    except Exception as exc:
        raise RuntimeError(f"Pesapal status check failed: {exc}") from exc
    if r.status_code >= 400:
        raise RuntimeError(data.get("message", "Unable to query Pesapal transaction"))
    return data


def _grant_payment_if_valid(tx_ref, tracking_id):
    plan_id = _plan_from_ref(tx_ref)
    if not plan_id or not tracking_id:
        return None
    status = _pesapal_status(tracking_id)
    status_text = str(status.get("payment_status_description", "")).upper()
    status_code = status.get("status_code")
    plan = PAYMENT_PLANS[plan_id]
    try:
        amount = float(status.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0.0
    with DB_LOCK:
        conn=db_conn()
        order=db_execute(conn, "SELECT * FROM payments WHERE tx_ref=?",(tx_ref,)).fetchone()
        conn.close()
    if not order:
        return {"paid":False,"status":"UNKNOWN","message":"KETS could not match this payment to an account."}
    expected_currency = str(order["currency"] or "UGX").upper()
    expected_amount = float(order["amount"] or 0)
    valid = (
        (status_code == 1 or status_text == "COMPLETED")
        and str(status.get("merchant_reference", "")) == tx_ref
        and str(status.get("currency", "")).upper() == expected_currency
        and abs(amount - expected_amount) < 0.01
    )
    with DB_LOCK:
        conn=db_conn()
        db_execute(conn, "UPDATE payments SET tracking_id=?,status=?,amount=?,updated_at=? WHERE tx_ref=?",
                     (tracking_id,"COMPLETED" if valid else (status_text or str(status_code or "PENDING")),amount,_now_iso(),tx_ref))
        conn.commit()
        conn.close()
    if not valid:
        return {"paid":False,"status":status_text or str(status_code or "UNKNOWN"),"message":"Payment is not yet confirmed or does not match this KETS plan."}
    token=_access_token(plan_id,tx_ref,order["user_id"])
    expires=get_eat_time()+datetime.timedelta(seconds=plan["seconds"])
    return {"paid":True,"token":token,"plan":plan_id,"expires":expires.isoformat(),"message":"Payment confirmed. KETS live signals are unlocked."}


@app.route("/api/access")
def api_access():
    user=_current_user()
    paid_override=os.environ.get("KETS_ACCESS","").lower()=="paid"
    access=_user_access(user["id"]) if user else None
    return jsonify({
        "authenticated":bool(user),
        "paid":bool(access or paid_override),
        "mode":"paid" if (access or paid_override) else "locked",
        "plan":access.get("plan") if access else ("admin" if paid_override else None),
        "expires":access.get("expires") if access else None,
        "user":_safe_user(user),
        "trading_hours_eat":"06:00-18:00",
        "provider":"Pesapal",
    })


@app.route("/api/plans")
def api_plans():
    return jsonify({
        "plans": {k: {"name":v["name"],"ugx":v["ugx"],"usd":v.get("usd"),"seconds":v["seconds"]} for k,v in PAYMENT_PLANS.items()},
        "payment_provider":"Pesapal","networks":["MTN","AIRTEL"],"currencies":["UGX","USD"]
    })


@app.route("/api/payments/create", methods=["POST"])
def api_payment_create():
    user = _current_user()
    if not user:
        return jsonify({"error":"Please sign in before purchasing a KETS plan."}),401
    body = request.get_json(silent=True) or {}
    plan_id = str(body.get("plan", "")).lower()
    phone = str(body.get("phone", "")).strip()
    email = str(body.get("email", "")).strip()
    network = str(body.get("network", "")).upper().strip()

    if plan_id not in PAYMENT_PLANS:
        return jsonify({"error": "Invalid plan"}), 400
    is_uganda = str(user.get("country_code", "")).upper() == "UG"
    plan = PAYMENT_PLANS[plan_id]
    if not is_uganda and not plan.get("usd"):
        return jsonify({"error": "This plan is currently available only to users in Uganda."}), 400
    if is_uganda:
        if network not in {"MTN", "AIRTEL"}:
            return jsonify({"error": "Choose MTN or AIRTEL"}), 400
        if not phone or len(re.sub(r"\D", "", phone)) < 9:
            return jsonify({"error": "Enter a valid Uganda mobile-money number"}), 400
    else:
        if not phone:
            phone = ""
        network = "INTERNATIONAL"
    if "@" not in email:
        return jsonify({"error": "Enter a valid email address"}), 400
    if email.lower() != user["email"].lower():
        return jsonify({"error":"Payment email must match your signed-in KETS account email."}),400

    # Uganda customers pay in UGX; customers outside Uganda pay the USD price.
    currency = "UGX" if is_uganda else "USD"
    amount_to_pay = float(plan["ugx"] if is_uganda else plan["usd"])
    tx_ref = f"KETS-{plan_id.upper()}-{secrets.token_hex(8)}"
    payload = {
        "id": tx_ref,
        "currency": currency,
        "amount": amount_to_pay,
        "description": f"KETS {plan['name']} signal access",
        "callback_url": PESAPAL_CALLBACK_URL,
        "cancellation_url": PESAPAL_CANCEL_URL,
        "notification_id": os.environ.get("PESAPAL_IPN_ID", "").strip(),
        "billing_address": {
            "email_address": email,
            "phone_number": phone,
            "country_code": str(user.get("country_code", "UG")).upper() or "UG",
            "first_name": "KETS",
            "last_name": "Customer",
            "line_1": "KETS Online",
            "city": "Kampala",
        },
    }

    if not payload["notification_id"]:
        return jsonify({
            "error": "PESAPAL_IPN_ID is not configured. Register the KETS IPN URL in Pesapal first."
        }), 503

    try:
        token = _pesapal_token()
        r = requests.post(
            f"{PESAPAL_BASE_URL}/Transactions/SubmitOrderRequest",
            headers=_pesapal_headers(token),
            json=payload,
            timeout=30,
        )
        data = r.json()
    except Exception as exc:
        return jsonify({"error": f"Pesapal connection failed: {exc}"}), 502

    if r.status_code >= 400 or not data.get("redirect_url"):
        return jsonify({"error": data.get("message", "Could not start Pesapal payment")}), 502

    tracking_id = data.get("order_tracking_id")
    with DB_LOCK:
        conn=db_conn()
        payment_values = (str(uuid.uuid4()),user["id"],tx_ref,tracking_id,plan_id,amount_to_pay,currency,"PENDING",network,email,phone,_now_iso(),_now_iso())
        if using_postgres():
            db_execute(conn, """INSERT INTO payments(id,user_id,tx_ref,tracking_id,plan,amount,currency,status,network,email,phone,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (tx_ref) DO UPDATE SET
                    user_id=EXCLUDED.user_id, tracking_id=EXCLUDED.tracking_id, plan=EXCLUDED.plan,
                    amount=EXCLUDED.amount, currency=EXCLUDED.currency, status=EXCLUDED.status,
                    network=EXCLUDED.network, email=EXCLUDED.email, phone=EXCLUDED.phone,
                    updated_at=EXCLUDED.updated_at""", payment_values)
        else:
            db_execute(conn, "INSERT OR REPLACE INTO payments(id,user_id,tx_ref,tracking_id,plan,amount,currency,status,network,email,phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", payment_values)
        conn.commit()
        conn.close()

    return jsonify({
        "ok": True,
        "tx_ref": tx_ref,
        "order_tracking_id": tracking_id,
        "redirect_url": data.get("redirect_url"),
        "status": data.get("status", "200"),
        "message": "Payment request created. Continue on the Pesapal payment page.",
    })


@app.route("/api/payments/verify", methods=["POST"])
def api_payment_verify():
    user = _current_user()
    if not user:
        return jsonify({"error":"Sign in required."}),401
    body = request.get_json(silent=True) or {}
    tx_ref = str(body.get("tx_ref", "")).strip()
    tracking_id = str(body.get("order_tracking_id", body.get("tracking_id", ""))).strip()

    plan_id = _plan_from_ref(tx_ref)
    if not plan_id:
        return jsonify({"error": "Invalid KETS payment reference"}), 400
    with DB_LOCK:
        conn=db_conn()
        owner=db_execute(conn, "SELECT user_id FROM payments WHERE tx_ref=?",(tx_ref,)).fetchone()
        conn.close()
    if not owner or owner["user_id"] != user["id"]:
        return jsonify({"error":"Payment does not belong to this account."}),403
    if not tracking_id:
        with DB_LOCK:
            conn=db_conn()
            row=db_execute(conn, "SELECT tracking_id FROM payments WHERE tx_ref=?",(tx_ref,)).fetchone()
            conn.close()
            tracking_id = str(row["tracking_id"]) if row and row["tracking_id"] else ""
    if not tracking_id:
        return jsonify({"error": "Missing Pesapal order tracking ID"}), 400

    try:
        result = _grant_payment_if_valid(tx_ref, tracking_id)
    except Exception as exc:
        return jsonify({"paid": False, "error": str(exc)}), 200
    return jsonify(result or {"paid": False, "message": "Payment could not be verified."})


@app.route("/api/payments/callback", methods=["GET"])
def api_payment_callback():
    # Pesapal redirects the customer here after payment. The callback does not
    # contain the final status; KETS queries GetTransactionStatus securely.
    tracking_id = request.args.get("OrderTrackingId", "").strip()
    tx_ref = request.args.get("OrderMerchantReference", "").strip()
    if not tx_ref or not tracking_id:
        return "Missing payment reference.", 400

    try:
        result = _grant_payment_if_valid(tx_ref, tracking_id)
    except Exception as exc:
        result = {"paid": False, "message": str(exc)}

    if result and result.get("paid"):
        with DB_LOCK:
            conn=db_conn()
            row=db_execute(conn, "SELECT user_id FROM payments WHERE tx_ref=?",(tx_ref,)).fetchone()
            conn.close()
        user_token=_user_token(row["user_id"]) if row else ""
        return f"""<!doctype html><html><head><meta charset='utf-8'><title>KETS Payment</title></head><body><p>Payment processed. Returning to KETS…</p><script>localStorage.setItem('kets_user_token', {json.dumps(user_token)}); window.location.replace('/?payment=success');</script></body></html>"""

    return f"""<!doctype html><html><head><meta charset='utf-8'><title>KETS Payment</title></head><body><p>{result.get('message', 'Payment is still being processed.') if result else 'Payment is still being processed.'}</p><p><a href='/' >Return to KETS</a></p></body></html>"""


@app.route("/api/payments/cancel", methods=["GET"])
def api_payment_cancel():
    return "Payment cancelled. You can return to KETS and try again.", 200


@app.route("/api/payments/ipn", methods=["GET", "POST"])
def api_payment_ipn():
    # Pesapal sends OrderTrackingId, OrderMerchantReference and
    # OrderNotificationType. IPN does not carry payment status, so query the
    # transaction status using the secure API before recording completion.
    data = request.get_json(silent=True) if request.is_json else request.values
    tracking_id = str(data.get("OrderTrackingId", "")).strip()
    tx_ref = str(data.get("OrderMerchantReference", "")).strip()
    notification_type = str(data.get("OrderNotificationType", "IPNCHANGE")).strip()

    if not tracking_id or not tx_ref:
        return jsonify({"error": "Missing Pesapal IPN parameters"}), 400

    try:
        result = _grant_payment_if_valid(tx_ref, tracking_id)
        if result and result.get("paid"):
            status = "COMPLETED"
        else:
            status = result.get("status", "PENDING") if result else "PENDING"
    except Exception as exc:
        print(f"⚠️ Pesapal IPN status check failed: {exc}")
        status = "PENDING"

    # Pesapal documents these values as the IPN acknowledgment payload.
    return jsonify({
        "orderNotificationType": notification_type,
        "orderTrackingId": tracking_id,
        "orderMerchantReference": tx_ref,
        "status": status,
    }), 200


@app.route("/api/payments/ipn-test", methods=["GET"])
def api_payment_ipn_test():
    return jsonify({
        "ok": True,
        "provider": "Pesapal",
        "ipn_url": PESAPAL_IPN_URL,
        "callback_url": PESAPAL_CALLBACK_URL,
        "environment": PESAPAL_ENVIRONMENT,
    })

def update_market_state(asset, symbol, candles, signal=None):
    if not candles:
        return
    c = candles[-1]
    with API_LOCK:
        MARKET_STATE[asset] = {
            "asset": asset,
            "symbol": symbol,
            "price": _num(c.get("close")),
            "open": _num(c.get("open")),
            "high": _num(c.get("high")),
            "low": _num(c.get("low")),
            "candle_time": c.get("datetime"),
            "candles": len(candles),
            "signal": signal.get("direction") if signal else None,
            "score": signal.get("score") if signal else None,
            "updated_at": get_eat_time().isoformat(),
        }


def _persist_signal(item):
    """Persist a signal in Render Postgres (or local SQLite fallback).

    The signal payload is stored as JSON so new dashboard fields can be added
    without requiring a schema migration for every field. Signal IDs are the
    idempotency key, preventing duplicate deliveries.
    """
    try:
        now = get_eat_time().isoformat()
        payload = json.dumps(item, separators=(",", ":"), default=str)
        with DB_LOCK:
            conn = db_conn()
            if using_postgres():
                db_execute(conn, """INSERT INTO signals(id,asset,direction,score,timestamp,payload,created_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT (id) DO UPDATE SET
                        payload=EXCLUDED.payload, asset=EXCLUDED.asset,
                        direction=EXCLUDED.direction, score=EXCLUDED.score,
                        timestamp=EXCLUDED.timestamp""",
                    (str(item.get("id")), str(item.get("asset", "UNKNOWN")),
                     str(item.get("direction", "")), float(item.get("score", 0)),
                     str(item.get("timestamp", now)), payload, now))
            else:
                db_execute(conn, """INSERT OR REPLACE INTO signals
                    (id,asset,direction,score,timestamp,payload,created_at)
                    VALUES(?,?,?,?,?,?,?)""",
                    (str(item.get("id")), str(item.get("asset", "UNKNOWN")),
                     str(item.get("direction", "")), float(item.get("score", 0)),
                     str(item.get("timestamp", now)), payload, now))
            conn.commit()
            conn.close()
        return True
    except Exception as exc:
        print(f"⚠️ Signal database persistence failed: {str(exc)[:300]}")
        return False


def _load_persistent_signals():
    """Load recent persisted signals, newest last."""
    cutoff = get_eat_time() - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
    try:
        with DB_LOCK:
            conn = db_conn()
            rows = db_execute(conn,
                "SELECT payload FROM signals WHERE timestamp >= ? ORDER BY timestamp ASC",
                (cutoff.isoformat(),)).fetchall()
            conn.close()
        result = []
        for row in rows:
            try:
                payload = row["payload"] if isinstance(row, dict) else row[0]
                result.append(json.loads(payload))
            except Exception:
                continue
        return result[-500:]
    except Exception as exc:
        print(f"⚠️ Signal database read failed: {str(exc)[:300]}")
        return []


def store_app_signal(asset, signal):
    now = get_eat_time()
    item = {
        "id": f"{asset}-{signal.get('direction')}-{now.timestamp()}",
        "asset": asset,
        "market": asset,
        "direction": signal.get("direction"),
        "score": signal.get("score"),
        "strength": signal.get("score"),
        "entry": signal.get("entry"),
        "price": signal.get("entry"),
        "current_price": signal.get("entry"),
        "take_profit": signal.get("take_profit"),
        "stop_loss": signal.get("stop_loss"),
        "price_move": signal.get("expected_move"),
        "price_move_pct": signal.get("expected_move_pct"),
        "expected_move": signal.get("expected_move"),
        "expected_move_pct": signal.get("expected_move_pct"),
        "estimated_duration": signal.get("estimated_duration"),
        "timestamp": now.isoformat(),
    }
    with API_LOCK:
        SIGNAL_HISTORY.append(item)
        cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
        SIGNAL_HISTORY[:] = [x for x in SIGNAL_HISTORY if datetime.datetime.fromisoformat(x["timestamp"]) >= cutoff]
    _persist_signal(item)
    return item


# ----------------------- TELEGRAM ----------------------------
def send_message(token, destination_id, message, destination_name):
    if not token or not destination_id:
        print(f"Telegram {destination_name}: missing configuration")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": destination_id, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        print(f"Telegram {destination_name}: {r.status_code} {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"Telegram error ({destination_name}): {e}")
        return False


def send_to_bot_and_channel(token, bot_chat_id, channel_id, bot_message, channel_message):
    a = send_message(token, bot_chat_id, bot_message, "BOT")
    b = send_message(token, channel_id, channel_message, "CHANNEL")
    return a or b


# ----------------------- INDICATORS --------------------------
def calculate_ema(prices, period):
    if not prices: return 0.0
    if len(prices) < period: return sum(prices) / len(prices)
    m = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]: ema = (p - ema) * m + ema
    return ema


def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0: return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def calculate_macd_series(prices):
    if len(prices) < 40: return None
    macd = []
    for i in range(26, len(prices) + 1):
        w = prices[:i]
        macd.append(calculate_ema(w, 12) - calculate_ema(w, 26))
    if len(macd) < 12: return None
    signal = [calculate_ema(macd[:i], 9) for i in range(9, len(macd) + 1)]
    if len(signal) < 2: return None
    return {"macd": macd[-1], "previous_macd": macd[-2], "signal": signal[-1], "previous_signal": signal[-2], "macd_values": macd, "signal_values": signal}


def recent_macd_cross(mv, sv, bullish=True, lookback=3):
    offset = len(mv) - len(sv)
    usable = min(lookback, len(mv)-1, len(sv)-1)
    for i in range(1, max(0, usable)+1):
        ci, pi = len(mv)-i, len(mv)-i-1
        csi, psi = ci-offset, pi-offset
        if min(csi, psi) < 0 or csi >= len(sv) or psi >= len(sv): continue
        cm, pm, cs, ps = mv[ci], mv[pi], sv[csi], sv[psi]
        if bullish and pm <= ps and cm > cs: return True
        if not bullish and pm >= ps and cm < cs: return True
    return False


def calculate_atr(candles, period=14):
    if len(candles) < period + 1: return 0.0
    tr = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i-1]
        tr.append(max(c["high"]-c["low"], abs(c["high"]-p["close"]), abs(c["low"]-p["close"])))
    atr = sum(tr[:period]) / period
    for x in tr[period:]: atr = (atr*(period-1)+x)/period
    return atr


def calculate_adx(candles, period=14):
    if len(candles) < period*2+1: return {"adx":0.0,"plus_di":0.0,"minus_di":0.0}
    trs=[]; pdm=[]; mdm=[]
    for i in range(1,len(candles)):
        c,p=candles[i],candles[i-1]
        up=c["high"]-p["high"]; down=p["low"]-c["low"]
        pdm.append(up if up>down and up>0 else 0.0); mdm.append(down if down>up and down>0 else 0.0)
        trs.append(max(c["high"]-c["low"],abs(c["high"]-p["close"]),abs(c["low"]-p["close"])))
    atr=sum(trs[:period])/period; plus=sum(pdm[:period])/period; minus=sum(mdm[:period])/period; dx=[]
    for i in range(period,len(trs)):
        atr=(atr*(period-1)+trs[i])/period; plus=(plus*(period-1)+pdm[i])/period; minus=(minus*(period-1)+mdm[i])/period
        pdi=100*plus/atr if atr else 0; mdi=100*minus/atr if atr else 0; den=pdi+mdi
        dx.append(100*abs(pdi-mdi)/den if den else 0)
    adx=sum(dx[:period])/min(period,len(dx)) if dx else 0
    for x in dx[period:]: adx=(adx*(period-1)+x)/period
    return {"adx":adx,"plus_di":100*plus/atr if atr else 0,"minus_di":100*minus/atr if atr else 0}


def aggregate_candles(candles, minutes):
    grouped={}
    for c in candles:
        try: dt=datetime.datetime.strptime(c["datetime"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            try: dt=datetime.datetime.fromisoformat(c["datetime"])
            except Exception: continue
        bucket=dt.replace(minute=(dt.minute//minutes)*minutes, second=0)
        k=bucket.strftime("%Y-%m-%d %H:%M:%S")
        if k not in grouped: grouped[k]={"datetime":k,"open":c["open"],"high":c["high"],"low":c["low"],"close":c["close"]}
        else:
            grouped[k]["high"]=max(grouped[k]["high"],c["high"]); grouped[k]["low"]=min(grouped[k]["low"],c["low"]); grouped[k]["close"]=c["close"]
    return list(grouped.values())


def timeframe_direction(candles):
    if len(candles)<3: return "NEUTRAL"
    x=[c["close"] for c in candles]
    if x[-1]>x[-2]>x[-3]: return "BULLISH"
    if x[-1]<x[-2]<x[-3]: return "BEARISH"
    f=calculate_ema(x,min(5,len(x))); s=sum(x)/len(x)
    return "BULLISH" if f>s else "BEARISH" if f<s else "NEUTRAL"


def candle_quality(c):
    r=c["high"]-c["low"]
    if r<=0:return {"quality":"INVALID","direction":"NEUTRAL","strength":0}
    body=abs(c["close"]-c["open"]); ratio=body/r
    d="BULLISH" if c["close"]>c["open"] else "BEARISH" if c["close"]<c["open"] else "NEUTRAL"
    q="STRONG " + d if ratio>=.70 and d!="NEUTRAL" else "GOOD " + d if ratio>=.45 and d!="NEUTRAL" else "WEAK " + d if d!="NEUTRAL" else "INDECISION"
    if ratio<.25:q="INDECISION / WEAK"
    return {"quality":q,"direction":d,"strength":round(ratio*100,1)}


def momentum_analysis(candles):
    if len(candles)<6:return {"direction":"NEUTRAL","state":"UNKNOWN","change":0}
    x=[c["close"] for c in candles]; a=x[-1]-x[-3]; b=x[-3]-x[-5]
    d="BULLISH" if a>0 else "BEARISH" if a<0 else "NEUTRAL"; s="ACCELERATING" if abs(a)>abs(b) else "WEAKENING" if abs(a)<abs(b) else "STABLE"
    return {"direction":d,"state":s,"change":a}


def find_levels(candles, lookback=20):
    s=candles[-lookback:]; return {"support":min(c["low"] for c in s),"resistance":max(c["high"] for c in s)}


def calculate_vwap(candles):
    if not all(c.get("volume") is not None for c in candles): return None
    tv=vv=0.0
    for c in candles:
        v=c.get("volume",0) or 0
        if v<=0:continue
        tv+=((c["high"]+c["low"]+c["close"])/3)*v; vv+=v
    return tv/vv if vv else None


def detect_market_regime(adx, atr, candles):
    if len(candles)<20:return "UNKNOWN"
    avg=sum(c["high"]-c["low"] for c in candles[-20:])/20
    if avg<=0:return "UNKNOWN"
    if adx>=25:return "TRENDING / HIGH VOLATILITY" if atr>avg*1.2 else "TRENDING"
    if atr<avg*.75:return "LOW VOLATILITY / RANGE"
    return "RANGE / TRANSITION"


def check_data_quality(candles):
    if len(candles)<40:return False,"Insufficient candles"
    for c in candles[-40:]:
        vals=[c["open"],c["high"],c["low"],c["close"]]
        if not all(math.isfinite(v) for v in vals):return False,"Invalid price data"
        if c["high"]<c["low"]:return False,"Invalid candle range"
    return True,"GOOD"


def check_overextension(price, ema9, atr):
    if atr<=0:return {"extended":False,"distance":0,"ratio":0}
    d=abs(price-ema9); r=d/atr
    return {"extended":r>=1.5,"distance":d,"ratio":r}


def fetch_1m_candles(symbol, api_key):
    if not api_key:return []
    try:
        r=requests.get("https://api.twelvedata.com/time_series",params={"symbol":symbol,"interval":"1min","outputsize":100,"timezone":"UTC","order":"asc","apikey":api_key},timeout=20)
        if r.status_code!=200:return []
        data=r.json()
        if data.get("status")=="error":
            print(f"Market API error {symbol}: {data.get('message')}"); return []
        out=[]
        for x in data.get("values",[]):
            try:
                c={"datetime":x["datetime"],"open":float(x["open"]),"high":float(x["high"]),"low":float(x["low"]),"close":float(x["close"]),"volume":None}
                if "volume" in x:
                    try:c["volume"]=float(x["volume"])
                    except Exception:pass
                out.append(c)
            except Exception:continue
        return out
    except Exception as e:
        print(f"Market data error {symbol}: {e}"); return []


def classify(score, extended):
    if extended:return "⚠️ EXTENDED — move may already be stretched."
    if score>=90:return "🔥 CONFIRMED ALIGNMENT"
    if score>=80:return "🟢 STRONG DEVELOPING SETUP"
    if score>=70:return "🟡 GOOD DEVELOPING SETUP"
    if score>=60:return "🔵 EARLY SETUP"
    return "⚪ DEVELOPING SETUP"


def interpretation(score, extended):
    if extended:return "⚠️ Setup is aligned, but price is extended."
    if score>=90:return "🔥 VERY STRONG ALIGNMENT — multiple independent factors agree."
    if score>=80:return "🟢 STRONG ALIGNMENT — trend, momentum and context agree."
    if score>=70:return "🟡 GOOD ALIGNMENT — early setup has several confirmations."
    if score>=60:return "🔵 EARLY SETUP — momentum is developing."
    return "⚪ DEVELOPING SETUP — early directional evidence is present."


def analyze_market(asset, symbol, candles):
    if len(candles)<40:return None
    ok,_=check_data_quality(candles)
    if not ok:return None
    closes=[c["close"] for c in candles]; cur,prev,prev2=candles[-1],candles[-2],candles[-3]; price=cur["close"]
    ema9=calculate_ema(closes,9); ema26=calculate_ema(closes,26); pe9=calculate_ema(closes[:-1],9); pe26=calculate_ema(closes[:-1],26)
    rsi=calculate_rsi(closes); prsi=calculate_rsi(closes[:-1]); md=calculate_macd_series(closes)
    if not md:return None
    cm,pm,cs,ps=md["macd"],md["previous_macd"],md["signal"],md["previous_signal"]
    bull_cross=pm<=ps and cm>cs; bear_cross=pm>=ps and cm<cs
    rb=recent_macd_cross(md["macd_values"],md["signal_values"],True,3); rs=recent_macd_cross(md["macd_values"],md["signal_values"],False,3)
    bull_macd=cm>cs; bear_macd=cm<cs; rising=cm>pm; falling=cm<pm
    bullish=cur["close"]>cur["open"]; bearish=cur["close"]<cur["open"]
    rising_price=price>prev["close"]>prev2["close"]; falling_price=price<prev["close"]<prev2["close"]
    hh=cur["high"]>prev["high"] and cur["low"]>prev["low"]; ll=cur["high"]<prev["high"] and cur["low"]<prev["low"]
    eb=ema9>ema26; es=ema9<ema26; ebc=pe9<=pe26 and ema9>ema26; esc=pe9>=pe26 and ema9<ema26
    pab=price>ema9; pbs=price<ema9; rr=rsi>prsi; rf=rsi<prsi
    buy=15*eb+10*pab+8*ebc+15*bull_macd+10*rising+15*bull_cross+12*(not bull_cross and rb)+8*(30<rsi<75)+5*rr+5*bullish+5*rising_price+4*hh
    sell=15*es+10*pbs+8*esc+15*bear_macd+10*falling+15*bear_cross+12*(not bear_cross and rs)+8*(25<rsi<70)+5*rf+5*bearish+5*falling_price+4*ll
    if buy>=sell and buy>=55: direction="BUY"; core=buy; reasons=[]
    elif sell>buy and sell>=55: direction="SELL"; core=sell; reasons=[]
    else:return None
    if eb and direction=="BUY":reasons.append("EMA9 > EMA26")
    if es and direction=="SELL":reasons.append("EMA9 < EMA26")
    if (pab and direction=="BUY") or (pbs and direction=="SELL"):reasons.append("Price aligned with EMA9")
    if (bull_macd and direction=="BUY") or (bear_macd and direction=="SELL"):reasons.append("MACD aligned")
    if (rising and direction=="BUY") or (falling and direction=="SELL"):reasons.append("MACD momentum")
    if (bull_cross and direction=="BUY") or (bear_cross and direction=="SELL"):reasons.append("Fresh MACD crossover")
    elif (rb and direction=="BUY") or (rs and direction=="SELL"):reasons.append("Recent MACD crossover")
    if (30<rsi<75 and direction=="BUY") or (25<rsi<70 and direction=="SELL"):reasons.append("RSI zone")
    if (rr and direction=="BUY") or (rf and direction=="SELL"):reasons.append("RSI momentum")
    if (bullish and direction=="BUY") or (bearish and direction=="SELL"):reasons.append("Directional candle")
    if (rising_price and direction=="BUY") or (falling_price and direction=="SELL"):reasons.append("Short-term momentum")
    if (hh and direction=="BUY") or (ll and direction=="SELL"):reasons.append("Market structure aligned")

    atr=calculate_atr(candles); ad=calculate_adx(candles); ci=candle_quality(cur); mom=momentum_analysis(candles); levels=find_levels(candles); vwap=calculate_vwap(candles)
    d5=timeframe_direction(aggregate_candles(candles,5)); d15=timeframe_direction(aggregate_candles(candles,15)); regime=detect_market_regime(ad["adx"],atr,candles); ext=check_overextension(price,ema9,atr); extended=ext["extended"]
    bonus=0; adv=[]
    if ad["adx"]>=25:
        aligned=(direction=="BUY" and ad["plus_di"]>ad["minus_di"]) or (direction=="SELL" and ad["minus_di"]>ad["plus_di"])
        bonus+=6 if aligned else -3; adv.append("ADX/DI aligned" if aligned else "ADX trend but DI conflict")
    elif ad["adx"]>=18:bonus+=2; adv.append("Developing trend strength")
    else:adv.append("Weak trend / ranging environment")
    if (direction=="BUY" and d5=="BULLISH") or (direction=="SELL" and d5=="BEARISH"):bonus+=5; adv.append("5M direction aligned")
    elif d5!="NEUTRAL":bonus-=2; adv.append("5M direction conflict")
    if (direction=="BUY" and d15=="BULLISH") or (direction=="SELL" and d15=="BEARISH"):bonus+=5; adv.append("15M direction aligned")
    elif d15!="NEUTRAL":bonus-=2; adv.append("15M direction conflict")
    if (direction=="BUY" and mom["direction"]=="BULLISH") or (direction=="SELL" and mom["direction"]=="BEARISH"):
        bonus+=3; adv.append("Momentum aligned")
        if mom["state"]=="ACCELERATING":bonus+=3; adv.append("Momentum accelerating")
        elif mom["state"]=="WEAKENING":bonus-=2; adv.append("Momentum weakening")
    if ci["direction"]==("BULLISH" if direction=="BUY" else "BEARISH") and ci["strength"]>=45:bonus+=3; adv.append("Candle quality aligned")
    if vwap is not None:
        if (direction=="BUY" and price>vwap) or (direction=="SELL" and price<vwap):bonus+=3; adv.append("VWAP aligned")
        else:bonus-=1; adv.append("VWAP conflict")
    if atr>0:
        room=(levels["resistance"]-price) if direction=="BUY" else (price-levels["support"])
        if room>atr:bonus+=3; adv.append("Room to key level")
        else:bonus-=3; adv.append("Key level nearby")
    if regime.startswith("TRENDING"):bonus+=3; adv.append("Trend-friendly regime")
    if extended:bonus-=6; adv.append("Price overextended from EMA9")
    score=max(0,min(100,int(core+bonus)))

    recent_lows=[c["low"] for c in candles[-6:-1]]; recent_highs=[c["high"] for c in candles[-6:-1]]
    entry=price
    if direction=="BUY":
        sl=min(recent_lows); risk=entry-sl
        if risk<=0:return None
        tp=entry+risk*2
    else:
        sl=max(recent_highs); risk=sl-entry
        if risk<=0:return None
        tp=entry-risk*2
    move=abs(tp-entry); move_pct=move/entry*100 if entry else 0
    ranges=[c["high"]-c["low"] for c in candles[-10:] if c["high"]>c["low"]]
    duration="Unable to estimate"
    if ranges:
        est=max(1,abs(tp-entry)/(sum(ranges)/len(ranges))); lo=max(1,int(est*.7)); hi=max(lo+1,int(est*1.3)); duration=f"{lo}-{hi} minutes"
    ts=get_eat_time().isoformat()
    key=f"{asset}_{direction}_{candles[-1]['datetime']}"
    if last_signal.get(asset)==key:return None
    last_signal[asset]=key
    interp=interpretation(score,extended); setup=classify(score,extended)
    macd_status="Fresh crossover" if (bull_cross or bear_cross) else "Recent crossover" if ((direction=="BUY" and rb) or (direction=="SELL" and rs)) else "Momentum aligned"
    bot=(f"🤖 *KETS — EARLY ENTRY SIGNAL — {asset}*\n━━━━━━━━━━━━━━━━━━\n📈 *Direction:* {'🟢 BUY / LONG' if direction=='BUY' else '🔴 SELL / SHORT'}\n💯 *Signal Strength:* {score}%\n🧠 *Interpretation:* {interp}\n🏷️ *Setup:* {setup}\n━━━━━━━━━━━━━━━━━━\n📍 *Market Price:* ${entry:,.2f}\n🎯 *Take Profit:* ${tp:,.2f}\n🛑 *Stop Loss:* ${sl:,.2f}\n📊 *Expected Price Move:* ${move:,.2f} ({move_pct:.2f}%)\n⏱️ *Estimated Duration:* {duration}\n━━━━━━━━━━━━━━━━━━\n📊 *1-MIN CHECK*\n├ EMA9: ${ema9:,.2f}\n├ EMA26: ${ema26:,.2f}\n├ RSI(14): {rsi:.2f}\n├ MACD: {cm:.5f}\n├ Signal: {cs:.5f}\n└ MACD Status: {macd_status}\n━━━━━━━━━━━━━━━━━━\n🧠 *INTELLIGENCE*\n├ Regime: {regime}\n├ ADX: {ad['adx']:.2f}\n├ DI+: {ad['plus_di']:.2f}\n├ DI-: {ad['minus_di']:.2f}\n├ ATR: ${atr:,.2f}\n├ Momentum: {mom['direction']} / {mom['state']}\n├ Candle: {ci['quality']}\n├ 5M: {d5}\n├ 15M: {d15}\n└ VWAP: {'$'+format(vwap,',.2f') if vwap is not None else 'Unavailable'}\n━━━━━━━━━━━━━━━━━━\n🎯 *LEVELS*\n├ Support: ${levels['support']:,.2f}\n└ Resistance: ${levels['resistance']:,.2f}\n━━━━━━━━━━━━━━━━━━\n🔎 *CORE:*\n" + "\n".join("• "+x for x in reasons) + "\n━━━━━━━━━━━━━━━━━━\n🧠 *ADVANCED:*\n" + "\n".join("• "+x for x in adv) + f"\n━━━━━━━━━━━━━━━━━━\n⏰ {ts}\n⚠️ Strategy-alignment score, not win probability.")
    channel=(f"🤖 *KETS — EARLY ENTRY SIGNAL — {asset}*\n━━━━━━━━━━━━━━━━━━\n📈 *Direction:* {'🟢 BUY / LONG' if direction=='BUY' else '🔴 SELL / SHORT'}\n💯 *Signal Strength:* {score}%\n🧠 *Interpretation:* {interp}\n━━━━━━━━━━━━━━━━━━\n📍 *Market Price:* ${entry:,.2f}\n🎯 *Take Profit:* ${tp:,.2f}\n🛑 *Stop Loss:* ${sl:,.2f}\n📊 *Expected Price Move:* ${move:,.2f} ({move_pct:.2f}%)\n⏱️ *Estimated Duration:* {duration}\n━━━━━━━━━━━━━━━━━━\n⏰ {ts}\n⚠️ Strategy-alignment score, not win probability.")
    return {"bot":bot,"channel":channel,"direction":direction,"score":score,"entry":entry,"take_profit":tp,"stop_loss":sl,"expected_move":move,"expected_move_pct":move_pct,"estimated_duration":duration,"timestamp":ts}


# ------------------------- ENGINE ----------------------------
def build_startup_messages():
    b="🤖 *KETS STRATEGY ENGINE ONLINE*\n━━━━━━━━━━━━━━━━━━\n✅ Backend connected\n📊 Timeframe: 1 minute\n🔄 Scan interval: 2 minutes\n⏰ Trading hours: 06:00-18:00 EAT\n💰 Weekdays: GOLD + BTC\n₿ Weekend: BTC ONLY\n🧠 Advanced intelligence ON\n━━━━━━━━━━━━━━━━━━\nℹ️ Strength is strategy alignment, not guaranteed win probability."
    c="🤖 *KETS STRATEGY ENGINE ONLINE*\n━━━━━━━━━━━━━━━━━━\n✅ Signal system online\n📊 1-minute monitoring\n🔄 Analysis every 2 minutes\n⏰ Active: 06:00-18:00 EAT\n⚡ Early-entry detection ON\n━━━━━━━━━━━━━━━━━━\n📡 KETS is monitoring the market."
    return b,c


def run_strategy():
    global last_scan, next_scan
    token=os.environ.get("TELEGRAM_BOT_TOKEN"); bot_id=os.environ.get("TELEGRAM_CHAT_ID"); channel_id=os.environ.get("TELEGRAM_CHANNEL_ID"); key=os.environ.get("TWELVE_DATA_API_KEY")
    print("🚀 KETS Strategy Engine started — 1M / 2M")
    sb,sc=build_startup_messages(); send_to_bot_and_channel(token,bot_id,channel_id,sb,sc)
    while True:
        started=time.time()
        try:
            now=get_eat_time(); last_scan=now.isoformat()
            if not trading_hours_open():
                time.sleep(120); continue
            for asset,symbol in get_markets().items():
                candles=fetch_1m_candles(symbol,key)
                if not candles:
                    with API_LOCK: MARKET_STATE[asset]={"asset":asset,"symbol":symbol,"status":"NO_DATA","updated_at":get_eat_time().isoformat()}
                    continue
                signal=analyze_market(asset,symbol,candles)
                update_market_state(asset,symbol,candles,signal)
                if signal:
                    store_app_signal(asset,signal)
                    send_to_bot_and_channel(token,bot_id,channel_id,signal["bot"],signal["channel"])
                print(f"🔎 {asset}: ${candles[-1]['close']:,.2f} | signal={signal['direction'] if signal else 'NONE'}")
        except Exception as e:
            print(f"⚠️ KETS engine error: {e}")
            try:
                send_to_bot_and_channel(token,bot_id,channel_id,f"⚠️ *KETS ENGINE ERROR*\n`{str(e)[:500]}`\n🔄 Engine will continue.","⚠️ *KETS SYSTEM NOTICE*\nA temporary system issue was detected.\n🔄 Monitoring will continue.")
            except Exception: pass
        sleep_time=max(1,120-(time.time()-started)); next_scan=(get_eat_time()+datetime.timedelta(seconds=sleep_time)).isoformat(); time.sleep(sleep_time)


# Gunicorn imports this module and does not execute __main__. Start the engine
# during module import, exactly once per worker.
engine_started = False
if os.environ.get("KETS_DISABLE_ENGINE", "0") != "1":
    engine_started = True
    Thread(target=run_strategy, daemon=True, name="kets-strategy-engine").start()

# Always run the source bridge unless explicitly disabled. This is independent
# of browser traffic and guarantees the website actively requests source signals.
source_bridge_started = False
if os.environ.get("KETS_DISABLE_SOURCE_BRIDGE", "0") != "1":
    source_bridge_started = True
    Thread(target=run_signal_source_bridge, daemon=True, name="kets-signal-source-bridge").start()

if __name__ == "__main__":
    run_strategy()
