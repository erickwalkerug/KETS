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

        # poll_source already calculated the target sleep
        # from its elapsed time. Recalculate safely here.
        time.sleep(
            max(
                1,
                POLL_SECONDS
                - 0
            )
        )


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

@app.route("/")
def home():

    base_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    index_path = os.path.join(
        base_dir,
        "index.html"
    )

    if os.path.exists(index_path):

        return send_from_directory(
            base_dir,
            "index.html"
        )

    return (
        "KETS APP ONLINE — "
        "connected to original trading bot"
    )


@app.route("/<path:filename>")
def frontend_files(filename):

    base_dir = os.path.dirname(
        os.path.abspath(__file__)
    )

    requested = os.path.join(
        base_dir,
        filename
    )

    if os.path.isfile(requested):

        return send_from_directory(
            base_dir,
            filename
        )

    return jsonify({
        "error": "file_not_found"
    }), 404


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
