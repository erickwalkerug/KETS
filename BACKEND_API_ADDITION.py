# KETS BACKEND API ADDITION
# Add this to your existing bot.py.
# Do NOT replace your strategy functions with this file.
#
# This uses the existing Flask app object and a 7-day in-memory signal store.
# For persistent history across Render restarts, use a database later.

from threading import Lock

SIGNAL_HISTORY = []
SIGNAL_HISTORY_LOCK = Lock()
SIGNAL_HISTORY_DAYS = 7

def store_app_signal(asset, signal):
    now = get_eat_time()
    item = {
        "id": f"{asset}-{signal.get('direction')}-{now.timestamp()}",
        "asset": asset,
        "market": asset,
        "direction": signal.get("direction"),
        "score": signal.get("score"),
        "entry": signal.get("entry"),
        "take_profit": signal.get("take_profit"),
        "stop_loss": signal.get("stop_loss"),
        "timestamp": now.isoformat()
    }
    with SIGNAL_HISTORY_LOCK:
        SIGNAL_HISTORY.append(item)
        cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
        SIGNAL_HISTORY[:] = [
            x for x in SIGNAL_HISTORY
            if datetime.datetime.fromisoformat(x["timestamp"]) >= cutoff
        ]
    return item

@app.route("/api/status")
def api_status():
    return {
        "status": "online",
        "refresh_interval_seconds": 120,
        "history_days": 7,
        "trading_hours_eat": "06:00-18:00",
        "website_url": os.environ.get("KETS_WEBSITE_URL", "")
    }

@app.route("/api/signals")
def api_signals():
    now = get_eat_time()
    cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
    with SIGNAL_HISTORY_LOCK:
        SIGNAL_HISTORY[:] = [
            x for x in SIGNAL_HISTORY
            if datetime.datetime.fromisoformat(x["timestamp"]) >= cutoff
        ]
        return {"signals": list(reversed(SIGNAL_HISTORY))}

# IMPORTANT:
# In run_strategy(), immediately after:
#     if signal:
# add:
#     store_app_signal(asset, signal)
#
# This must happen before/around the Telegram send call.
