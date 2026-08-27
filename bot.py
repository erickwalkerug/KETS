import os, time, datetime, math
from threading import Thread, Lock
from flask import Flask, jsonify, send_from_directory, request
import requests
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

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
last_signal = {}


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
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "app.js")


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


@app.route("/api/signals")
def api_signals():
    # Customers authenticate here; the private bot is contacted server-to-server.
    if not web_access_paid():
        return jsonify({"error": "Live signals are locked. Purchase access to unlock them."}), 403

    source_url = os.environ.get("KETS_SIGNAL_SOURCE_URL", "").strip().rstrip("/")
    source_key = os.environ.get("KETS_SIGNAL_SOURCE_KEY", "").strip()
    if source_url and source_key:
        try:
            r = requests.get(
                source_url + "/api/signals",
                headers={"X-KETS-API-KEY": source_key, "Accept": "application/json"},
                timeout=15,
            )
            if r.status_code != 200:
                return jsonify({"error": "Signal source rejected the request.", "source_status": r.status_code}), 502
            data = r.json()
            data["source"] = "private KETS strategy engine"
            return jsonify(data)
        except Exception as exc:
            return jsonify({"error": "Unable to reach the private signal engine."}), 502

    # Fallback only for local/admin testing when no remote source is configured.
    now = get_eat_time()
    cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
    latest = {}
    with API_LOCK:
        clean = []
        for x in SIGNAL_HISTORY:
            try:
                dt = datetime.datetime.fromisoformat(x["timestamp"])
            except Exception:
                continue
            if dt >= cutoff:
                clean.append(x)
                latest[x.get("asset", x.get("market", "UNKNOWN"))] = x
        SIGNAL_HISTORY[:] = clean
    return jsonify({"signals": latest, "time_eat": now.isoformat(), "source": "website-local engine"})


@app.route("/api/history")
def api_history():
    now = get_eat_time()
    cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
    with API_LOCK:
        items = []
        for x in SIGNAL_HISTORY:
            try:
                if datetime.datetime.fromisoformat(x["timestamp"]) >= cutoff:
                    items.append(x)
            except Exception:
                pass
    return jsonify({"history": items[-500:]})



PAYMENT_PLANS = {
    "1_day": {"name": "1 Day", "ugx": 5000, "days": 1},
    "1_week": {"name": "1 Week", "ugx": 30000, "days": 7},
    "1_month": {"name": "1 Month", "ugx": 100000, "days": 30},
    "1_year": {"name": "1 Year", "ugx": 1000000, "days": 365},
}

def _payment_secret():
    return os.environ.get("KETS_SESSION_SECRET") or os.environ.get("FLW_SECRET_KEY") or "CHANGE_ME"

def _serializer():
    return URLSafeTimedSerializer(_payment_secret(), salt="kets-access-v1")

def _access_token(plan_id, tx_ref):
    plan = PAYMENT_PLANS[plan_id]
    expires = get_eat_time() + datetime.timedelta(days=plan["days"])
    payload = {"plan": plan_id, "tx_ref": tx_ref, "expires": expires.isoformat()}
    return _serializer().dumps(payload)

def _token_access():
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        token = request.cookies.get("kets_access", "")
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=366*24*3600)
        expiry = datetime.datetime.fromisoformat(data["expires"])
        if expiry > get_eat_time():
            return data
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        pass
    return None

def web_access_paid():
    # Real per-user access is granted by a signed token after Flutterwave
    # confirms a successful transaction. KETS_ACCESS=paid remains as an
    # emergency/admin override for the owner.
    if os.environ.get("KETS_ACCESS", "").lower() == "paid":
        return True
    return _token_access() is not None

def _flw_headers():
    key = os.environ.get("FLW_SECRET_KEY", "")
    if not key:
        raise RuntimeError("FLW_SECRET_KEY is not configured")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def _plan_from_ref(tx_ref):
    prefix = "KETS-"
    if not tx_ref.startswith(prefix):
        return None
    plan_id = tx_ref.split("-", 2)[1].lower()
    return plan_id if plan_id in PAYMENT_PLANS else None

@app.route("/api/access")
def api_access():
    access = _token_access()
    paid_override = os.environ.get("KETS_ACCESS", "").lower() == "paid"
    return jsonify({
        "paid": bool(access or paid_override),
        "mode": "paid" if (access or paid_override) else "locked",
        "plan": access.get("plan") if access else ("admin" if paid_override else None),
        "expires": access.get("expires") if access else None,
        "trading_hours_eat": "06:00-18:00",
        "provider": "Flutterwave",
    })

@app.route("/api/plans")
def api_plans():
    return jsonify({
        "plans": {
            k: {"name": v["name"], "ugx": v["ugx"], "usd": round(v["ugx"]/1000, 2)}
            for k, v in PAYMENT_PLANS.items()
        },
        "payment_provider": "Flutterwave",
        "networks": ["MTN", "AIRTEL"],
        "currency": "UGX",
    })

@app.route("/api/payments/create", methods=["POST"])
def api_payment_create():
    body = request.get_json(silent=True) or {}
    plan_id = str(body.get("plan", "")).lower()
    phone = str(body.get("phone", "")).strip()
    email = str(body.get("email", "")).strip()
    network = str(body.get("network", "")).upper().strip()

    if plan_id not in PAYMENT_PLANS:
        return jsonify({"error": "Invalid plan"}), 400
    if network not in {"MTN", "AIRTEL"}:
        return jsonify({"error": "Choose MTN or AIRTEL"}), 400
    if not phone or len(re.sub(r"\D", "", phone)) < 9:
        return jsonify({"error": "Enter a valid Uganda mobile-money number"}), 400
    if "@" not in email:
        return jsonify({"error": "Enter a valid email address"}), 400

    tx_ref = f"KETS-{plan_id.upper()}-{secrets.token_hex(8)}"
    plan = PAYMENT_PLANS[plan_id]
    payload = {
        "amount": plan["ugx"],
        "currency": "UGX",
        "email": email,
        "tx_ref": tx_ref,
        "phone_number": phone,
        "network": network,
        "order_id": tx_ref,
        "fullname": email.split("@")[0],
        "meta": {
            "product": "KETS signal access",
            "plan": plan_id,
        },
    }
    try:
        r = requests.post(
            "https://api.flutterwave.com/v3/charges?type=mobile_money_uganda",
            headers=_flw_headers(),
            json=payload,
            timeout=30,
        )
        data = r.json()
    except Exception as exc:
        return jsonify({"error": f"Payment provider connection failed: {exc}"}), 502

    if r.status_code >= 400 or data.get("status") != "success":
        return jsonify({"error": data.get("message", "Could not start payment")}), 502

    d = data.get("data") or {}
    return jsonify({
        "ok": True,
        "tx_ref": tx_ref,
        "transaction_id": d.get("id"),
        "status": d.get("status") or data.get("status"),
        "message": data.get("message", "Payment request created. Approve it on your phone."),
    })

@app.route("/api/payments/verify", methods=["POST"])
def api_payment_verify():
    body = request.get_json(silent=True) or {}
    tx_ref = str(body.get("tx_ref", "")).strip()
    transaction_id = body.get("transaction_id")

    plan_id = _plan_from_ref(tx_ref)
    if not plan_id:
        return jsonify({"error": "Invalid KETS payment reference"}), 400

    try:
        transaction_id = int(transaction_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Missing transaction ID"}), 400

    try:
        r = requests.get(
            f"https://api.flutterwave.com/v3/transactions/{transaction_id}/verify",
            headers=_flw_headers(),
            timeout=30,
        )
        data = r.json()
    except Exception as exc:
        return jsonify({"error": f"Verification connection failed: {exc}"}), 502

    if r.status_code >= 400 or data.get("status") != "success":
        return jsonify({"error": data.get("message", "Unable to verify payment yet"), "paid": False}), 200

    d = data.get("data") or {}
    plan = PAYMENT_PLANS[plan_id]
    valid = (
        d.get("status") == "successful"
        and str(d.get("tx_ref")) == tx_ref
        and str(d.get("currency", "")).upper() == "UGX"
        and float(d.get("amount", 0)) >= float(plan["ugx"])
    )
    if not valid:
        return jsonify({
            "paid": False,
            "status": d.get("status", "unknown"),
            "message": "Payment is not yet confirmed or does not match this KETS plan."
        }), 200

    token = _access_token(plan_id, tx_ref)
    return jsonify({
        "paid": True,
        "token": token,
        "plan": plan_id,
        "expires": _serializer().loads(token)["expires"],
        "message": "Payment confirmed. KETS live signals are unlocked.",
    })

@app.route("/api/payments/webhook", methods=["POST"])
def api_payment_webhook():
    # Webhook is intentionally acknowledgement-only. The browser verification
    # endpoint performs the authoritative transaction verification before value
    # is granted, following Flutterwave's recommended pattern.
    secret_hash = os.environ.get("FLW_WEBHOOK_SECRET_HASH", "")
    received = request.headers.get("verif-hash", "")
    if secret_hash and not secrets.compare_digest(received, secret_hash):
        return jsonify({"error": "Invalid webhook signature"}), 401
    return jsonify({"received": True}), 200

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

if __name__ == "__main__":
    run_strategy()
