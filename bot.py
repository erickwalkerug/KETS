import os
import time
import math
import threading
import datetime as dt
from collections import deque
from functools import wraps

import requests
from flask import Flask, jsonify, request

# ============================================================
# KETS BOT / API — Twelve Data powered
# ------------------------------------------------------------
# IMPORTANT:
# - Market indicators and strategy calculations stay backend-only.
# - Frontend receives final signals, not EMA/RSI/MACD/ADX/etc.
# - Put secrets in Render Environment Variables.
# ============================================================

app = Flask(__name__)

# Optional CORS. The API remains usable if flask-cors isn't installed.
try:
    from flask_cors import CORS
    CORS(app)
except Exception:
    pass

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()
API_SECRET = os.getenv("KETS_API_SECRET", "").strip()

# Optional Telegram settings. Leave empty if Telegram is not used.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()

# Twelve Data symbols can be overridden in Render if needed.
BTC_SYMBOL = os.getenv("BTC_SYMBOL", "BTC/USD").strip()
GOLD_SYMBOL = os.getenv("GOLD_SYMBOL", "XAU/USD").strip()

EAT = dt.timezone(dt.timedelta(hours=3))
SIGNAL_START = dt.time(6, 0)
SIGNAL_END = dt.time(18, 0)

SCAN_SECONDS = 60
BROADCAST_SECONDS = 120
HISTORY_DAYS = 7
HTTP_TIMEOUT = 20

# Prices/candles are cached briefly so a failed request doesn't destroy
# the whole API response.
data_cache = {}
signal_history = deque(maxlen=5000)
last_signals = {}
last_broadcast_at = 0.0
engine_lock = threading.Lock()


def now_eat():
    return dt.datetime.now(EAT)


def within_signal_hours(t=None):
    t = t or now_eat()
    current = t.time()
    return SIGNAL_START <= current < SIGNAL_END


def clean_old_history():
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=HISTORY_DAYS)
    kept = deque(maxlen=signal_history.maxlen)
    for item in signal_history:
        try:
            ts = dt.datetime.fromisoformat(item["timestamp"])
            if ts >= cutoff:
                kept.append(item)
        except Exception:
            continue
    signal_history.clear()
    signal_history.extend(kept)


def api_auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        # If no secret is configured, keep local testing easy.
        # Production Render deployment should set KETS_API_SECRET.
        if API_SECRET:
            supplied = request.headers.get("X-KETS-API-KEY", "")
            if supplied != API_SECRET:
                return jsonify({"ok": False, "error": "unauthorized"}), 401
        return fn(*args, **kwargs)
    return wrapper


def twelve_data(endpoint, params):
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not configured")

    query = dict(params)
    query["apikey"] = TWELVE_DATA_API_KEY
    url = f"https://api.twelvedata.com/{endpoint}"

    response = requests.get(url, params=query, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(data.get("message", "Twelve Data error"))

    return data


def fetch_candles(symbol, interval="1min", outputsize=300):
    key = (symbol, interval, outputsize)
    try:
        data = twelve_data(
            "time_series",
            {
                "symbol": symbol,
                "interval": interval,
                "outputsize": outputsize,
                "format": "JSON",
                "order": "ASC",
            },
        )
        values = data.get("values", [])
        if not values:
            raise RuntimeError(f"No candle data returned for {symbol}")

        candles = []
        for x in values:
            candles.append(
                {
                    "time": x["datetime"],
                    "open": float(x["open"]),
                    "high": float(x["high"]),
                    "low": float(x["low"]),
                    "close": float(x["close"]),
                    "volume": float(x.get("volume", 0) or 0),
                }
            )
        data_cache[key] = {"at": time.time(), "candles": candles}
        return candles
    except Exception:
        cached = data_cache.get(key)
        if cached:
            return cached["candles"]
        raise


def ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    out = sum(values[:period]) / period
    for value in values[period:]:
        out = value * k + out * (1 - k)
    return out


def ema_series(values, period):
    if len(values) < period:
        return [None] * len(values)
    result = [None] * (period - 1)
    current = sum(values[:period]) / period
    result.append(current)
    k = 2.0 / (period + 1)
    for value in values[period:]:
        current = value * k + current * (1 - k)
        result.append(current)
    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None, []
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    series = [None] * period

    def calc(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100.0 - (100.0 / (1.0 + rs))

    series.append(calc(avg_gain, avg_loss))

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
        series.append(calc(avg_gain, avg_loss))

    return series[-1], series


def macd(values, fast_period=12, slow_period=26, signal_period=9):
    if len(values) < slow_period + signal_period:
        return None, None, []

    fast = ema_series(values, fast_period)
    slow = ema_series(values, slow_period)
    macd_line = []
    for a, b in zip(fast, slow):
        macd_line.append(None if a is None or b is None else a - b)

    usable = [x for x in macd_line if x is not None]
    signal_series = ema_series(usable, signal_period)
    signal_map = [None] * (len(macd_line) - len(signal_series)) + signal_series

    pairs = []
    for m, s in zip(macd_line, signal_map):
        if m is not None and s is not None:
            pairs.append((m, s))

    if not pairs:
        return None, None, []
    return pairs[-1][0], pairs[-1][1], pairs


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        c = candles[i]
        prev_close = candles[i - 1]["close"]
        tr = max(
            c["high"] - c["low"],
            abs(c["high"] - prev_close),
            abs(c["low"] - prev_close),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def adx_and_di(candles, period=14):
    # Wilder-style ADX approximation.
    if len(candles) < period * 2 + 2:
        return None, None, None

    trs, plus_dm, minus_dm = [], [], []
    for i in range(1, len(candles)):
        cur, prev = candles[i], candles[i - 1]
        up = cur["high"] - prev["high"]
        down = prev["low"] - cur["low"]

        plus = up if up > down and up > 0 else 0
        minus = down if down > up and down > 0 else 0
        tr = max(
            cur["high"] - cur["low"],
            abs(cur["high"] - prev["close"]),
            abs(cur["low"] - prev["close"]),
        )
        trs.append(tr)
        plus_dm.append(plus)
        minus_dm.append(minus)

    def wilder(seq, p):
        value = sum(seq[:p])
        out = [None] * (p - 1) + [value]
        for x in seq[p:]:
            value = value - value / p + x
            out.append(value)
        return out

    tr_s = wilder(trs, period)
    p_s = wilder(plus_dm, period)
    m_s = wilder(minus_dm, period)

    dx = []
    last_pdi = last_mdi = None
    for t, p, m in zip(tr_s, p_s, m_s):
        if t in (None, 0):
            dx.append(None)
            continue
        pdi = 100 * p / t
        mdi = 100 * m / t
        last_pdi, last_mdi = pdi, mdi
        denom = pdi + mdi
        dx.append(0 if denom == 0 else 100 * abs(pdi - mdi) / denom)

    usable_dx = [x for x in dx if x is not None]
    if len(usable_dx) < period:
        return None, last_pdi, last_mdi

    adx_value = sum(usable_dx[-period:]) / period
    return adx_value, last_pdi, last_mdi


def direction_from_candles(candles, lookback=5):
    if len(candles) < lookback + 1:
        return "NEUTRAL"
    start = candles[-lookback - 1]["close"]
    end = candles[-1]["close"]
    if end > start:
        return "BULLISH"
    if end < start:
        return "BEARISH"
    return "NEUTRAL"


def structure(candles):
    if len(candles) < 8:
        return "INSUFFICIENT DATA"
    recent = candles[-6:]
    first_half = recent[:3]
    second_half = recent[3:]
    h1 = max(x["high"] for x in first_half)
    h2 = max(x["high"] for x in second_half)
    l1 = min(x["low"] for x in first_half)
    l2 = min(x["low"] for x in second_half)

    if h2 < h1 and l2 < l1:
        return "Lower High + Lower Low"
    if h2 > h1 and l2 > l1:
        return "Higher High + Higher Low"
    return "MIXED"


def candle_quality(c):
    rng = max(c["high"] - c["low"], 1e-12)
    body = abs(c["close"] - c["open"])
    body_ratio = body / rng
    if c["close"] < c["open"] and body_ratio >= 0.65:
        return "STRONG BEARISH"
    if c["close"] > c["open"] and body_ratio >= 0.65:
        return "STRONG BULLISH"
    if c["close"] < c["open"]:
        return "BEARISH"
    if c["close"] > c["open"]:
        return "BULLISH"
    return "DOJI"


def support_resistance(candles, lookback=30):
    sample = candles[-lookback:]
    support = min(x["low"] for x in sample)
    resistance = max(x["high"] for x in sample)
    return support, resistance


def analyze(symbol, label):
    candles_1m = fetch_candles(symbol, "1min", 300)
    closes = [x["close"] for x in candles_1m]
    price = closes[-1]

    e9 = ema(closes, 9)
    e26 = ema(closes, 26)
    rsi_value, rsi_series = rsi(closes, 14)
    macd_value, macd_signal, macd_pairs = macd(closes)
    atr_value = atr(candles_1m, 14)
    adx_value, di_plus, di_minus = adx_and_di(candles_1m, 14)
    struct = structure(candles_1m)
    candle = candle_quality(candles_1m[-1])

    # 5M/15M are independently fetched and used for confirmation.
    candles_5m = fetch_candles(symbol, "5min", 100)
    candles_15m = fetch_candles(symbol, "15min", 100)
    dir5 = direction_from_candles(candles_5m)
    dir15 = direction_from_candles(candles_15m)

    support, resistance = support_resistance(candles_1m)
    distance_support = abs(price - support)
    distance_resistance = abs(resistance - price)

    bullish = 0
    bearish = 0
    bull_reasons = []
    bear_reasons = []

    if e9 is not None and e26 is not None:
        if e9 > e26:
            bullish += 1
            bull_reasons.append("EMA9 > EMA26")
        elif e9 < e26:
            bearish += 1
            bear_reasons.append("EMA9 < EMA26")

    if e9 is not None:
        if price > e9:
            bullish += 1
            bull_reasons.append("Price above EMA9")
        elif price < e9:
            bearish += 1
            bear_reasons.append("Price below EMA9")

    if macd_value is not None and macd_signal is not None:
        if macd_value > macd_signal:
            bullish += 1
            bull_reasons.append("MACD bullish")
        elif macd_value < macd_signal:
            bearish += 1
            bear_reasons.append("MACD bearish")

        if len(macd_pairs) >= 2:
            prev_macd, prev_signal = macd_pairs[-2]
            if macd_value > prev_macd:
                bullish += 1
                bull_reasons.append("MACD rising")
            elif macd_value < prev_macd:
                bearish += 1
                bear_reasons.append("MACD falling")

    if rsi_value is not None:
        if rsi_value >= 55:
            bullish += 1
            bull_reasons.append("RSI bullish zone")
        elif rsi_value <= 45:
            bearish += 1
            bear_reasons.append("RSI sell zone")

        if len(rsi_series) >= 2 and rsi_series[-2] is not None:
            if rsi_value > rsi_series[-2]:
                bullish += 1
                bull_reasons.append("RSI rising")
            elif rsi_value < rsi_series[-2]:
                bearish += 1
                bear_reasons.append("RSI falling")

    if candles_1m[-1]["close"] > candles_1m[-1]["open"]:
        bullish += 1
        bull_reasons.append("Bullish candle")
    elif candles_1m[-1]["close"] < candles_1m[-1]["open"]:
        bearish += 1
        bear_reasons.append("Bearish candle")

    if struct == "Higher High + Higher Low":
        bullish += 2
        bull_reasons.append("Higher High + Higher Low")
    elif struct == "Lower High + Lower Low":
        bearish += 2
        bear_reasons.append("Lower High + Lower Low")

    if dir5 == "BULLISH":
        bullish += 2
        bull_reasons.append("5M direction aligned")
    elif dir5 == "BEARISH":
        bearish += 2
        bear_reasons.append("5M direction aligned")

    if dir15 == "BULLISH":
        bullish += 2
        bull_reasons.append("15M direction aligned")
    elif dir15 == "BEARISH":
        bearish += 2
        bear_reasons.append("15M direction aligned")

    if adx_value is not None:
        if adx_value >= 25:
            if di_plus is not None and di_minus is not None:
                if di_plus > di_minus:
                    bullish += 3
                    bull_reasons.append("ADX trend + DI bullish")
                elif di_minus > di_plus:
                    bearish += 3
                    bear_reasons.append("ADX trend + DI bearish")

    total = bullish + bearish
    if total == 0:
        direction = "WAIT"
        strength = 0
        reasons = []
    else:
        direction = "BUY" if bullish > bearish else "SELL" if bearish > bullish else "WAIT"
        winning = max(bullish, bearish)
        strength = round(min(100, 50 + (winning / max(total, 1)) * 50))
        reasons = bull_reasons if direction == "BUY" else bear_reasons

    # Conservative early-entry filter: only broadcast a directional signal
    # when several independent factors align.
    aligned = (
        direction in ("BUY", "SELL")
        and strength >= 60
        and ((direction == "BUY" and bullish >= 5) or
             (direction == "SELL" and bearish >= 5))
    )

    if not aligned:
        direction = "WAIT"

    regime = "TRENDING" if adx_value is not None and adx_value >= 25 else "RANGING"

    # TP/SL use ATR internally; the formula is not returned to clients.
    move = max((atr_value or price * 0.001) * 4.0, price * 0.0005)
    risk = max((atr_value or price * 0.001) * 2.0, price * 0.00025)

    if direction == "SELL":
        tp = price - move
        sl = price + risk
        expected_move = abs(price - tp)
    elif direction == "BUY":
        tp = price + move
        sl = price - risk
        expected_move = abs(tp - price)
    else:
        tp = sl = expected_move = None

    # Public object intentionally contains no indicators/tools.
    return {
        "market": label,
        "symbol": symbol,
        "direction": direction,
        "strength": int(strength),
        "price": round(price, 2),
        "take_profit": round(tp, 2) if tp is not None else None,
        "stop_loss": round(sl, 2) if sl is not None else None,
        "expected_move": round(expected_move, 2) if expected_move is not None else None,
        "estimated_duration": "4-8 minutes" if direction != "WAIT" else None,
        "setup": "CONFIRMED ALIGNMENT" if aligned else "NO SIGNAL",
        "regime": regime,
        "advanced_intelligence": True,
        "early_entry_detection": True,
        "strength_scoring": True,
        "timestamp": now_eat().isoformat(),
        "conditions": reasons if aligned else [],
    }


def public_signal(signal):
    # Do not expose internal strategy calculations.
    allowed = {
        "market", "symbol", "direction", "strength", "price",
        "take_profit", "stop_loss", "expected_move",
        "estimated_duration", "setup", "regime",
        "advanced_intelligence", "early_entry_detection",
        "strength_scoring", "timestamp"
    }
    return {k: signal.get(k) for k in allowed}


def telegram_send(text, chat_id):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=HTTP_TIMEOUT,
        )
        return r.ok
    except Exception:
        return False


def signal_text(s):
    return (
        f"🤖 KETS — EARLY ENTRY SIGNAL — {s['market']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📈 Direction: {s['direction']}\n"
        f"💯 Signal Strength: {s['strength']}%\n"
        f"🏷️ Setup: {s['setup']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📍 Market Price: ${s['price']:,.2f}\n"
        f"🎯 Take Profit: ${s['take_profit']:,.2f}\n"
        f"🛑 Stop Loss: ${s['stop_loss']:,.2f}\n"
        f"📊 Expected Price Move: ${s['expected_move']:,.2f}\n"
        f"⏱️ Estimated Duration: {s['estimated_duration']}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🧠 Market Regime: {s['regime']}\n"
        f"⏰ Time: {s['timestamp']}\n"
        f"⚠️ Strength is an alignment score, not a guaranteed win probability."
    )


def market_update(btc, gold):
    return (
        "🤖 KETS — 2-MINUTE MARKET UPDATE\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 KETS BTC: {'SIGNAL SENT' if btc['direction'] != 'WAIT' else 'NO SIGNAL'}\n"
        f"📈 Direction: {btc['direction']}\n"
        f"💯 Strength: {btc['strength']}%\n"
        f"📍 Price: ${btc['price']:,.2f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🎯 KETS GOLD: {'SIGNAL SENT' if gold['direction'] != 'WAIT' else 'NO SIGNAL'}\n"
        f"📈 Direction: {gold['direction']}\n"
        f"💯 Strength: {gold['strength']}%\n"
        f"📍 Price: ${gold['price']:,.2f}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⏰ {now_eat().strftime('%Y-%m-%d %H:%M:%S EAT')}\n"
        "🧠 Advanced intelligence: ON\n"
        "⚡ Early-entry detection: ON\n"
        "💯 Strength scoring: ON\n"
        "📡 Destination: BOT + CHANNEL\n"
        "🔄 Next scan: ~2 minutes"
    )


def engine_loop():
    global last_broadcast_at
    while True:
        started = time.time()
        try:
            clean_old_history()

            if within_signal_hours():
                with engine_lock:
                    btc = analyze(BTC_SYMBOL, "BTC")
                    gold = analyze(GOLD_SYMBOL, "GOLD")

                    last_signals["BTC"] = public_signal(btc)
                    last_signals["GOLD"] = public_signal(gold)

                    for s in (btc, gold):
                        if s["direction"] != "WAIT":
                            # Avoid adding duplicate identical signals every minute.
                            previous = signal_history[-1] if signal_history else None
                            if not previous or not (
                                previous.get("market") == s["market"]
                                and previous.get("direction") == s["direction"]
                                and previous.get("price") == public_signal(s)["price"]
                            ):
                                signal_history.append(public_signal(s))

                            # Telegram detailed signal.
                            if TELEGRAM_CHAT_ID:
                                telegram_send(signal_text(s), TELEGRAM_CHAT_ID)

                    if time.time() - last_broadcast_at >= BROADCAST_SECONDS:
                        text = market_update(btc, gold)
                        if TELEGRAM_CHAT_ID:
                            telegram_send(text, TELEGRAM_CHAT_ID)
                        if TELEGRAM_CHANNEL_ID and TELEGRAM_CHANNEL_ID != TELEGRAM_CHAT_ID:
                            telegram_send(text, TELEGRAM_CHANNEL_ID)
                        last_broadcast_at = time.time()

        except Exception as exc:
            app.logger.exception("KETS engine error: %s", exc)

        elapsed = time.time() - started
        time.sleep(max(1, SCAN_SECONDS - elapsed))


@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "service": "KETS Signal API",
        "status": "online",
        "market_data": "Twelve Data",
        "signal_hours": "06:00-18:00 EAT",
        "history_days": HISTORY_DAYS,
        "indicators_public": False,
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "status": "awake",
        "time_eat": now_eat().isoformat(),
        "within_signal_hours": within_signal_hours(),
        "twelve_data_configured": bool(TWELVE_DATA_API_KEY),
    })


@app.get("/api/signals")
@api_auth_required
def api_signals():
    clean_old_history()
    active = within_signal_hours()
    return jsonify({
        "ok": True,
        "active": active,
        "signals": {
            "BTC": last_signals.get("BTC"),
            "GOLD": last_signals.get("GOLD"),
        } if active else {},
        "server_time": now_eat().isoformat(),
        "next_broadcast": next_broadcast_seconds(),
    })


@app.get("/api/history")
@api_auth_required
def api_history():
    clean_old_history()
    # History is intentionally public-safe and limited to the last 7 days.
    return jsonify({
        "ok": True,
        "history_days": HISTORY_DAYS,
        "history": list(signal_history),
    })


@app.get("/api/status")
@api_auth_required
def api_status():
    now = now_eat()
    stop = dt.datetime.combine(now.date(), SIGNAL_END, tzinfo=EAT)
    start = dt.datetime.combine(now.date(), SIGNAL_START, tzinfo=EAT)

    if now < start:
        seconds_to_start = int((start - now).total_seconds())
    else:
        seconds_to_start = 0

    if now < stop:
        seconds_to_stop = int((stop - now).total_seconds())
    else:
        seconds_to_stop = 0

    return jsonify({
        "ok": True,
        "signal_window": {
            "start": "06:00 EAT",
            "end": "18:00 EAT",
            "active": within_signal_hours(now),
            "seconds_to_start": seconds_to_start,
            "seconds_to_stop": seconds_to_stop,
        },
        "next_broadcast_seconds": next_broadcast_seconds(),
        "server_time": now.isoformat(),
    })


def next_broadcast_seconds():
    if last_broadcast_at <= 0:
        return BROADCAST_SECONDS
    return max(0, int(BROADCAST_SECONDS - (time.time() - last_broadcast_at)))


# ============================================================
# Subscription API
# ------------------------------------------------------------
# These are plan definitions and server-side entitlement hooks.
# Payment verification must be connected to a real payment provider
# before automatically marking an account paid.
# ============================================================

PLANS = {
    "1_day": {
        "name": "1 Day",
        "ugx": 5000,
        "usd": 5,
        "days": 1,
    },
    "1_week": {
        "name": "1 Week",
        "ugx": 30000,
        "usd": 30,
        "days": 7,
    },
    "1_month": {
        "name": "1 Month",
        "ugx": 100000,
        "usd": 100,
        "days": 30,
    },
    "1_year": {
        "name": "1 Year",
        "ugx": 1000000,
        "usd": 1000,
        "days": 365,
    },
}


@app.get("/api/plans")
def plans():
    return jsonify({
        "ok": True,
        "plans": PLANS,
        "uganda": {
            "currency": "UGX",
            "mtn": "+256791058183",
            "airtel": "+256747427556",
        },
        "international": {
            "currency": "USD",
            "bank_account": "9030028492447",
        },
        "signal_hours": "06:00-18:00 EAT",
    })


# Frontend can use this endpoint to display the locked state.
# A real authenticated user/payment service should replace the placeholder.
@app.get("/api/access")
@api_auth_required
def access():
    return jsonify({
        "ok": True,
        "paid": False,
        "signals_locked": True,
        "message": "Payment verification is required before live signals are unlocked.",
    })


def start_engine():
    thread = threading.Thread(target=engine_loop, daemon=True)
    thread.start()


if __name__ == "__main__":
    start_engine()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, threaded=True)
