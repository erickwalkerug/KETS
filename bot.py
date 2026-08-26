import os
import time
import math
import datetime as dt
from threading import Thread, Lock

import requests
from flask import Flask, jsonify, render_template_string

try:
    from flask_cors import CORS
except Exception:
    CORS = None


# ============================================================
# KETS — SELF-AWAKE 1M DATA / 2M SCAN BACKEND
#
# Important:
# - Render starts Flask with gunicorn or python bot.py.
# - The dashboard is built into this file, so it does not depend
#   on a missing/broken index.html.
# - Scans 24/7 by default. Set KETS_TRADING_HOURS_ONLY=true if
#   you want the old 06:00-18:00 EAT restriction.
# - TWELVE_DATA_API_KEY is required for Twelve Data market data.
# ============================================================

app = Flask(__name__)
if CORS:
    try:
        CORS(app)
    except Exception:
        pass

API_LOCK = Lock()
SIGNAL_HISTORY = []
MARKET_STATE = {}
LAST_SIGNAL_KEY = {}
last_scan = None
next_scan = None
engine_started = False
engine_error = None

HISTORY_DAYS = 7
SCAN_SECONDS = 120
TRADING_HOURS_ONLY = os.environ.get("KETS_TRADING_HOURS_ONLY", "false").lower() == "true"


# ============================================================
# TIME / CONFIG
# ============================================================

def get_eat_time():
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)


def trading_hours_open():
    if not TRADING_HOURS_ONLY:
        return True
    t = get_eat_time().time()
    return dt.time(6, 0) <= t < dt.time(18, 0)


def get_markets():
    # BTC every day; GOLD Monday-Friday.
    if get_eat_time().weekday() >= 5:
        return {"BTC": "BTC/USD"}
    return {"BTC": "BTC/USD", "GOLD": "XAU/USD"}


def num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def public_url():
    return (
        os.environ.get("KETS_PUBLIC_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
        or "https://kets.onrender.com"
    ).rstrip("/")


# ============================================================
# SELF-AWAKE
# ============================================================

def self_awake():
    """
    Periodic health request.

    NOTE: A process cannot guarantee that a Render free web service
    stays awake after Render suspends it. This heartbeat helps while
    the process is running. For a true 24/7 service, use a paid
    always-on Render service or an external monitor/cron.
    """
    url = public_url()

    while True:
        try:
            r = requests.get(f"{url}/api/health", timeout=15)
            print(f"💓 Self-awake heartbeat: HTTP {r.status_code}")
        except Exception as exc:
            print(f"💓 Self-awake heartbeat failed: {exc}")
        time.sleep(300)


# ============================================================
# BUILT-IN WEB APP
# ============================================================

HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KETS Early Entry Signals</title>
<style>
body{font-family:Arial,sans-serif;margin:0;background:#0b1020;color:#eef2ff}
header{padding:18px;background:#111936;position:sticky;top:0}
h1{margin:0 0 6px;font-size:21px}
.small{color:#aab4d0;font-size:13px}
.wrap{padding:14px;max-width:900px;margin:auto}
.card{background:#121a36;border:1px solid #26335f;border-radius:14px;padding:14px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.value{font-size:20px;font-weight:bold;margin-top:5px}
.signal{border-left:5px solid #5b8cff}
.buy{border-left-color:#20c878}
.sell{border-left-color:#ff5964}
.muted{color:#9ca8c7}
.badge{display:inline-block;padding:5px 9px;border-radius:999px;background:#26335f;font-size:12px}
table{width:100%;border-collapse:collapse}
td{padding:7px;border-bottom:1px solid #26335f}
</style>
</head>
<body>
<header>
  <h1>🤖 KETS Early Entry Signals</h1>
  <div class="small" id="top">Connecting...</div>
</header>
<div class="wrap">
  <div class="grid">
    <div class="card"><div class="small">SYSTEM</div><div class="value" id="system">Connecting</div></div>
    <div class="card"><div class="small">LAST SCAN</div><div class="value" id="lastscan">—</div></div>
    <div class="card"><div class="small">NEXT SCAN</div><div class="value" id="nextscan">—</div></div>
    <div class="card"><div class="small">SIGNALS</div><div class="value" id="count">0</div></div>
  </div>

  <div class="card">
    <b>Markets</b>
    <div id="markets" class="muted">Waiting for market data...</div>
  </div>

  <div class="card">
    <b>Latest Signals</b>
    <div id="signals" class="muted">No signals yet.</div>
  </div>
</div>

<script>
async function getJSON(url){
  const r=await fetch(url,{cache:"no-store"});
  if(!r.ok) throw new Error("HTTP "+r.status);
  return await r.json();
}

function esc(x){
  return String(x ?? "").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[m]));
}

async function refresh(){
  try{
    const s=await getJSON("/api/status");
    document.getElementById("system").textContent=s.status || "online";
    document.getElementById("lastscan").textContent=s.last_scan || "—";
    document.getElementById("nextscan").textContent=s.next_scan || "—";
    document.getElementById("count").textContent=s.signal_count ?? 0;
    document.getElementById("top").textContent=
      "Online • 1-minute data • 2-minute scan • Updated "+(s.time_eat||"");

    const m=await getJSON("/api/market");
    const entries=Object.values(m.markets||{});
    document.getElementById("markets").innerHTML=entries.length
      ? entries.map(x=>`
        <div class="card">
          <b>${esc(x.asset)} — ${esc(x.symbol)}</b>
          <div class="value">${x.price==null?"NO DATA":"$"+Number(x.price).toLocaleString()}</div>
          <div class="small">
            ${esc(x.status||"LIVE")} •
            Signal: ${esc(x.signal||"NONE")} •
            Score: ${esc(x.score??"—")} •
            Updated: ${esc(x.updated_at||"—")}
          </div>
        </div>`).join("")
      : "Waiting for market data...";

    const h=await getJSON("/api/signals");
    const sig=h.signals||[];
    document.getElementById("signals").innerHTML=sig.length
      ? sig.slice(0,20).map(x=>`
        <div class="card signal ${x.direction==="BUY"?"buy":"sell"}">
          <b>${x.direction==="BUY"?"🟢 BUY":"🔴 SELL"} — ${esc(x.asset)}</b>
          <div>Score: <b>${esc(x.score)}%</b></div>
          <div>Entry: $${Number(x.entry).toLocaleString()}</div>
          <div>TP: $${Number(x.take_profit).toLocaleString()}</div>
          <div>SL: $${Number(x.stop_loss).toLocaleString()}</div>
          <div class="small">${esc(x.timestamp)}</div>
        </div>`).join("")
      : "No signals yet.";
  }catch(e){
    document.getElementById("system").textContent="Backend error";
    document.getElementById("top").textContent="API error: "+e.message;
  }
}

refresh();
setInterval(refresh,15000);
</script>
</body>
</html>
"""


@app.route("/")
def home():
    return render_template_string(HTML)


@app.route("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "status": "online",
        "engine_started": engine_started,
        "time_eat": get_eat_time().isoformat(),
    })


@app.route("/api/status")
def api_status():
    with API_LOCK:
        return jsonify({
            "status": "online" if engine_started else "starting",
            "engine_started": engine_started,
            "refresh_interval_seconds": SCAN_SECONDS,
            "history_days": HISTORY_DAYS,
            "trading_hours_only": TRADING_HOURS_ONLY,
            "trading_hours_eat": "06:00-18:00" if TRADING_HOURS_ONLY else "24/7",
            "last_scan": last_scan,
            "next_scan": next_scan,
            "markets": list(MARKET_STATE.keys()),
            "signal_count": len(SIGNAL_HISTORY),
            "time_eat": get_eat_time().isoformat(),
            "engine_error": engine_error,
        })


@app.route("/api/market")
def api_market():
    with API_LOCK:
        return jsonify({
            "markets": MARKET_STATE,
            "time_eat": get_eat_time().isoformat()
        })


@app.route("/api/signals")
def api_signals():
    cutoff = get_eat_time() - dt.timedelta(days=HISTORY_DAYS)
    with API_LOCK:
        SIGNAL_HISTORY[:] = [
            x for x in SIGNAL_HISTORY
            if dt.datetime.fromisoformat(x["timestamp"]) >= cutoff
        ]
        return jsonify({"signals": list(reversed(SIGNAL_HISTORY))})


# ============================================================
# TELEGRAM
# ============================================================

def send_message(token, destination_id, message, destination_name):
    if not token or not destination_id:
        print(f"Telegram {destination_name}: not configured")
        return False

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": destination_id,
                "text": message,
                "parse_mode": "Markdown"
            },
            timeout=15,
        )
        print(f"Telegram {destination_name}: {r.status_code}")
        return r.status_code == 200
    except Exception as exc:
        print(f"Telegram {destination_name} error: {exc}")
        return False


def send_to_bot_and_channel(token, bot_chat_id, channel_id, bot_message, channel_message):
    a = send_message(token, bot_chat_id, bot_message, "BOT")
    b = send_message(token, channel_id, channel_message, "CHANNEL")
    return a or b


# ============================================================
# INDICATORS
# ============================================================

def ema(prices, period):
    if not prices:
        return 0.0
    if len(prices) < period:
        return sum(prices) / len(prices)

    multiplier = 2 / (period + 1)
    value = sum(prices[:period]) / period

    for price in prices[period:]:
        value = (price - value) * multiplier + value

    return value


def rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(prices):
    if len(prices) < 40:
        return None

    values = []
    for i in range(26, len(prices) + 1):
        window = prices[:i]
        values.append(ema(window, 12) - ema(window, 26))

    if len(values) < 12:
        return None

    signal_values = []
    for i in range(9, len(values) + 1):
        signal_values.append(ema(values[:i], 9))

    if len(signal_values) < 2:
        return None

    return {
        "macd": values[-1],
        "previous_macd": values[-2],
        "signal": signal_values[-1],
        "previous_signal": signal_values[-2],
        "values": values,
        "signal_values": signal_values,
    }


def atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0

    tr = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        tr.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"])
        ))

    value = sum(tr[:period]) / period

    for x in tr[period:]:
        value = (value * (period - 1) + x) / period

    return value


def adx(candles, period=14):
    if len(candles) < period * 2 + 1:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}

    trs, plus_dm, minus_dm = [], [], []

    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        up = c["high"] - p["high"]
        down = p["low"] - c["low"]

        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"])
        ))

    a = sum(trs[:period]) / period
    p = sum(plus_dm[:period]) / period
    m = sum(minus_dm[:period]) / period
    dx = []

    for i in range(period, len(trs)):
        a = (a * (period - 1) + trs[i]) / period
        p = (p * (period - 1) + plus_dm[i]) / period
        m = (m * (period - 1) + minus_dm[i]) / period

        pdi = 100 * p / a if a else 0
        mdi = 100 * m / a if a else 0
        total = pdi + mdi

        dx.append(100 * abs(pdi - mdi) / total if total else 0)

    if not dx:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}

    value = sum(dx[:period]) / min(period, len(dx))
    for x in dx[period:]:
        value = (value * (period - 1) + x) / period

    return {
        "adx": value,
        "plus_di": 100 * p / a if a else 0,
        "minus_di": 100 * m / a if a else 0,
    }


def aggregate(candles, minutes):
    grouped = {}

    for c in candles:
        try:
            stamp = dt.datetime.fromisoformat(c["datetime"])
        except Exception:
            continue

        bucket = stamp.replace(
            minute=(stamp.minute // minutes) * minutes,
            second=0,
            microsecond=0
        )
        key = bucket.strftime("%Y-%m-%d %H:%M:%S")

        if key not in grouped:
            grouped[key] = {
                "datetime": key,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
            }
        else:
            grouped[key]["high"] = max(grouped[key]["high"], c["high"])
            grouped[key]["low"] = min(grouped[key]["low"], c["low"])
            grouped[key]["close"] = c["close"]

    return list(grouped.values())


def direction(candles):
    if len(candles) < 3:
        return "NEUTRAL"

    closes = [c["close"] for c in candles]

    if closes[-1] > closes[-2] > closes[-3]:
        return "BULLISH"
    if closes[-1] < closes[-2] < closes[-3]:
        return "BEARISH"

    fast = ema(closes, min(5, len(closes)))
    slow = sum(closes) / len(closes)

    if fast > slow:
        return "BULLISH"
    if fast < slow:
        return "BEARISH"
    return "NEUTRAL"


def candle_quality(c):
    spread = c["high"] - c["low"]
    if spread <= 0:
        return {"quality": "INVALID", "direction": "NEUTRAL", "strength": 0}

    body = abs(c["close"] - c["open"])
    ratio = body / spread

    if c["close"] > c["open"]:
        d = "BULLISH"
    elif c["close"] < c["open"]:
        d = "BEARISH"
    else:
        d = "NEUTRAL"

    if ratio >= 0.70 and d != "NEUTRAL":
        q = "STRONG " + d
    elif ratio >= 0.45 and d != "NEUTRAL":
        q = "GOOD " + d
    elif d != "NEUTRAL":
        q = "WEAK " + d
    else:
        q = "INDECISION"

    if ratio < 0.25:
        q = "INDECISION / WEAK"

    return {
        "quality": q,
        "direction": d,
        "strength": round(ratio * 100, 1)
    }


def momentum(candles):
    if len(candles) < 6:
        return {"direction": "NEUTRAL", "state": "UNKNOWN"}

    x = [c["close"] for c in candles]
    a = x[-1] - x[-3]
    b = x[-3] - x[-5]

    d = "BULLISH" if a > 0 else "BEARISH" if a < 0 else "NEUTRAL"
    state = (
        "ACCELERATING" if abs(a) > abs(b)
        else "WEAKENING" if abs(a) < abs(b)
        else "STABLE"
    )

    return {"direction": d, "state": state}


def levels(candles, lookback=20):
    subset = candles[-lookback:]
    return {
        "support": min(c["low"] for c in subset),
        "resistance": max(c["high"] for c in subset)
    }


def vwap(candles):
    if not all(c.get("volume") is not None for c in candles):
        return None

    total_value = 0.0
    total_volume = 0.0

    for c in candles:
        volume = c.get("volume", 0) or 0
        if volume <= 0:
            continue

        typical = (c["high"] + c["low"] + c["close"]) / 3
        total_value += typical * volume
        total_volume += volume

    return total_value / total_volume if total_volume else None


# ============================================================
# MARKET DATA
# ============================================================

def fetch_1m_candles(symbol, api_key):
    if not api_key:
        print("⚠️ TWELVE_DATA_API_KEY is missing")
        return []

    try:
        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": symbol,
                "interval": "1min",
                "outputsize": 100,
                "timezone": "UTC",
                "order": "asc",
                "apikey": api_key,
            },
            timeout=20,
        )

        if r.status_code != 200:
            print(f"Market API HTTP {r.status_code} for {symbol}")
            return []

        data = r.json()

        if data.get("status") == "error":
            print(f"Market API error {symbol}: {data.get('message')}")
            return []

        result = []

        for item in data.get("values", []):
            try:
                result.append({
                    "datetime": item["datetime"],
                    "open": float(item["open"]),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "close": float(item["close"]),
                    "volume": (
                        float(item["volume"])
                        if item.get("volume") not in (None, "")
                        else None
                    ),
                })
            except Exception:
                continue

        return result

    except Exception as exc:
        print(f"Market data error {symbol}: {exc}")
        return []


# ============================================================
# STRATEGY
# ============================================================

def analyze_market(asset, symbol, candles):
    if len(candles) < 40:
        return None

    closes = [c["close"] for c in candles]
    price = candles[-1]["close"]
    current = candles[-1]
    previous = candles[-2]
    previous2 = candles[-3]

    ema9 = ema(closes, 9)
    ema26 = ema(closes, 26)
    previous_ema9 = ema(closes[:-1], 9)
    previous_ema26 = ema(closes[:-1], 26)

    current_rsi = rsi(closes)
    previous_rsi = rsi(closes[:-1])

    md = macd(closes)
    if not md:
        return None

    cm = md["macd"]
    pm = md["previous_macd"]
    cs = md["signal"]
    ps = md["previous_signal"]

    bull_cross = pm <= ps and cm > cs
    bear_cross = pm >= ps and cm < cs

    bull_macd = cm > cs
    bear_macd = cm < cs

    rising_macd = cm > pm
    falling_macd = cm < pm

    bullish_candle = current["close"] > current["open"]
    bearish_candle = current["close"] < current["open"]

    rising_price = price > previous["close"] > previous2["close"]
    falling_price = price < previous["close"] < previous2["close"]

    higher_structure = (
        current["high"] > previous["high"]
        and current["low"] > previous["low"]
    )
    lower_structure = (
        current["high"] < previous["high"]
        and current["low"] < previous["low"]
    )

    ema_bull = ema9 > ema26
    ema_bear = ema9 < ema26

    price_above_ema = price > ema9
    price_below_ema = price < ema9

    rsi_rising = current_rsi > previous_rsi
    rsi_falling = current_rsi < previous_rsi

    buy = (
        15 * ema_bull
        + 10 * price_above_ema
        + 8 * (previous_ema9 <= previous_ema26 and ema9 > ema26)
        + 15 * bull_macd
        + 10 * rising_macd
        + 15 * bull_cross
        + 8 * (30 < current_rsi < 75)
        + 5 * rsi_rising
        + 5 * bullish_candle
        + 5 * rising_price
        + 4 * higher_structure
    )

    sell = (
        15 * ema_bear
        + 10 * price_below_ema
        + 8 * (previous_ema9 >= previous_ema26 and ema9 < ema26)
        + 15 * bear_macd
        + 10 * falling_macd
        + 15 * bear_cross
        + 8 * (25 < current_rsi < 70)
        + 5 * rsi_falling
        + 5 * bearish_candle
        + 5 * falling_price
        + 4 * lower_structure
    )

    if buy >= sell and buy >= 55:
        direction_value = "BUY"
        core = buy
    elif sell > buy and sell >= 55:
        direction_value = "SELL"
        core = sell
    else:
        return None

    atr_value = atr(candles)
    adx_value = adx(candles)
    candle = candle_quality(current)
    mom = momentum(candles)
    lv = levels(candles)
    vw = vwap(candles)

    d5 = direction(aggregate(candles, 5))
    d15 = direction(aggregate(candles, 15))

    bonus = 0

    if adx_value["adx"] >= 25:
        aligned = (
            direction_value == "BUY"
            and adx_value["plus_di"] > adx_value["minus_di"]
        ) or (
            direction_value == "SELL"
            and adx_value["minus_di"] > adx_value["plus_di"]
        )
        bonus += 6 if aligned else -3

    if (direction_value == "BUY" and d5 == "BULLISH") or (
        direction_value == "SELL" and d5 == "BEARISH"
    ):
        bonus += 5

    if (direction_value == "BUY" and d15 == "BULLISH") or (
        direction_value == "SELL" and d15 == "BEARISH"
    ):
        bonus += 5

    if (direction_value == "BUY" and mom["direction"] == "BULLISH") or (
        direction_value == "SELL" and mom["direction"] == "BEARISH"
    ):
        bonus += 3
        if mom["state"] == "ACCELERATING":
            bonus += 3
        elif mom["state"] == "WEAKENING":
            bonus -= 2

    if candle["direction"] == (
        "BULLISH" if direction_value == "BUY" else "BEARISH"
    ) and candle["strength"] >= 45:
        bonus += 3

    if vw is not None:
        if (direction_value == "BUY" and price > vw) or (
            direction_value == "SELL" and price < vw
        ):
            bonus += 3
        else:
            bonus -= 1

    score = max(0, min(100, int(core + bonus)))

    # Recent 5 candles for practical SL/TP.
    recent_lows = [c["low"] for c in candles[-6:-1]]
    recent_highs = [c["high"] for c in candles[-6:-1]]

    entry = price

    if direction_value == "BUY":
        stop = min(recent_lows)
        risk = entry - stop
        if risk <= 0:
            return None
        take_profit = entry + risk * 2
    else:
        stop = max(recent_highs)
        risk = stop - entry
        if risk <= 0:
            return None
        take_profit = entry - risk * 2

    key = f"{asset}_{direction_value}_{candles[-1]['datetime']}"

    with API_LOCK:
        if LAST_SIGNAL_KEY.get(asset) == key:
            return None
        LAST_SIGNAL_KEY[asset] = key

    timestamp = get_eat_time().isoformat()

    return {
        "asset": asset,
        "market": asset,
        "direction": direction_value,
        "score": score,
        "entry": entry,
        "take_profit": take_profit,
        "stop_loss": stop,
        "timestamp": timestamp,
        "rsi": current_rsi,
        "ema9": ema9,
        "ema26": ema26,
        "macd": cm,
        "macd_signal": cs,
        "adx": adx_value["adx"],
        "plus_di": adx_value["plus_di"],
        "minus_di": adx_value["minus_di"],
        "atr": atr_value,
        "5m": d5,
        "15m": d15,
    }


# ============================================================
# STORAGE
# ============================================================

def update_market_state(asset, symbol, candles, signal=None):
    if not candles:
        return

    latest = candles[-1]

    with API_LOCK:
        MARKET_STATE[asset] = {
            "asset": asset,
            "symbol": symbol,
            "price": num(latest.get("close")),
            "open": num(latest.get("open")),
            "high": num(latest.get("high")),
            "low": num(latest.get("low")),
            "candle_time": latest.get("datetime"),
            "candles": len(candles),
            "signal": signal.get("direction") if signal else None,
            "score": signal.get("score") if signal else None,
            "status": "LIVE",
            "updated_at": get_eat_time().isoformat(),
        }


def store_signal(signal):
    now = get_eat_time()

    item = dict(signal)
    item["id"] = f"{signal['asset']}-{signal['direction']}-{now.timestamp()}"

    with API_LOCK:
        SIGNAL_HISTORY.append(item)

        cutoff = now - dt.timedelta(days=HISTORY_DAYS)
        SIGNAL_HISTORY[:] = [
            x for x in SIGNAL_HISTORY
            if dt.datetime.fromisoformat(x["timestamp"]) >= cutoff
        ]


# ============================================================
# ENGINE
# ============================================================

def run_strategy():
    global last_scan, next_scan, engine_started, engine_error

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    bot_id = os.environ.get("TELEGRAM_CHAT_ID")
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID")
    api_key = os.environ.get("TWELVE_DATA_API_KEY")

    engine_started = True

    print("🚀 KETS ENGINE ONLINE")
    print(f"📡 Scan interval: {SCAN_SECONDS}s")
    print(f"⏰ Trading restriction: {'06:00-18:00 EAT' if TRADING_HOURS_ONLY else '24/7'}")
    print(f"🔑 Twelve Data key: {'FOUND' if api_key else 'MISSING'}")

    while True:
        started = time.time()

        try:
            now = get_eat_time()
            last_scan = now.isoformat()
            engine_error = None

            if not trading_hours_open():
                print("⏸️ Outside configured trading hours.")
            else:
                markets = get_markets()

                for asset, symbol in markets.items():
                    candles = fetch_1m_candles(symbol, api_key)

                    if not candles:
                        with API_LOCK:
                            MARKET_STATE[asset] = {
                                "asset": asset,
                                "symbol": symbol,
                                "status": "NO_DATA",
                                "updated_at": get_eat_time().isoformat(),
                            }
                        continue

                    signal = analyze_market(asset, symbol, candles)
                    update_market_state(asset, symbol, candles, signal)

                    if signal:
                        store_signal(signal)

                        text = (
                            f"🤖 KETS SIGNAL — {asset}\n"
                            f"Direction: {signal['direction']}\n"
                            f"Score: {signal['score']}%\n"
                            f"Entry: ${signal['entry']:,.2f}\n"
                            f"TP: ${signal['take_profit']:,.2f}\n"
                            f"SL: ${signal['stop_loss']:,.2f}\n"
                            f"Time: {get_eat_time().strftime('%Y-%m-%d %H:%M:%S EAT')}"
                        )

                        send_to_bot_and_channel(
                            token, bot_id, channel_id, text, text
                        )

                    print(
                        f"🔎 {asset}: "
                        f"${candles[-1]['close']:,.2f} | "
                        f"signal={signal['direction'] if signal else 'NONE'}"
                    )

        except Exception as exc:
            engine_error = str(exc)[:500]
            print(f"⚠️ KETS ENGINE ERROR: {engine_error}")

        sleep_for = max(1, SCAN_SECONDS - (time.time() - started))
        next_scan = (
            get_eat_time() + dt.timedelta(seconds=sleep_for)
        ).isoformat()

        time.sleep(sleep_for)


# ============================================================
# STARTUP
# ============================================================

def start_background_workers():
    # Prevent duplicate workers if the module is imported more than once.
    if getattr(app, "_kets_workers_started", False):
        return

    app._kets_workers_started = True

    Thread(target=run_strategy, daemon=True, name="kets-engine").start()
    Thread(target=self_awake, daemon=True, name="kets-heartbeat").start()

    print("✅ KETS background workers started")


if __name__ == "__main__":
    start_background_workers()
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
else:
    # Required when Render starts the app with gunicorn bot:app.
    start_background_workers()
