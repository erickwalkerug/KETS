import os, time, datetime, math, re, sqlite3, hashlib, base64, json, uuid, asyncio
from threading import Thread, Lock
from flask import Flask, jsonify, send_from_directory, request, redirect, make_response
import requests
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from cryptography.fernet import Fernet

# Flask application exposed to Gunicorn.
# Must be created before any @app.route decorators below.
app = Flask(__name__)

# Runtime state used by the dashboard and signal engine.
# These values do not change the trading strategy; they only keep the API state
# available to the web UI and survive Gunicorn module import correctly.
API_LOCK = Lock()
MARKET_STATE = {}
SIGNAL_HISTORY = []
SIGNAL_HISTORY_DAYS = 7
last_signal = {}
last_scan = None
next_scan = None

EAT = datetime.timezone(datetime.timedelta(hours=3))

def get_eat_time():
    return datetime.datetime.now(datetime.timezone.utc).astimezone(EAT)

def _num(value, default=0.0):
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default

def trading_hours_open(now=None):
    now = now or get_eat_time()
    return datetime.time(6, 0) <= now.time() < datetime.time(18, 0)

def get_markets(now=None):
    # KETS schedule: weekdays = GOLD only; weekends = BTC only.
    now = now or get_eat_time()
    if now.weekday() < 5:
        return {"GOLD": "XAU/USD"}
    return {"BTC": "BTC/USD"}

def _source_config():
    return (
        os.environ.get("KETS_SIGNAL_SOURCE_URL", "").strip().rstrip("/"),
        os.environ.get("KETS_SIGNAL_SOURCE_KEY", "").strip()
        or os.environ.get("KETS_API_KEY", "").strip()
        or os.environ.get("KETS_SIGNALS_API_KEY", "").strip(),
    )

def _source_request_authorized():
    """Authorize private bot -> website signal sync without requiring a paid user session."""
    supplied = request.headers.get("X-KETS-API-KEY", "").strip()
    expected = os.environ.get("KETS_SIGNAL_RECEIVER_KEY", "").strip() or _source_config()[1]
    return bool(expected) and bool(supplied) and secrets.compare_digest(supplied, expected)

@app.route("/api/health", methods=["GET"])
def api_health():
    try:
        with DB_LOCK:
            conn=db_conn()
            conn.execute("SELECT 1").fetchone()
            conn.close()
        return jsonify({"ok":True,"service":"KETS","database_configured":True,"database_mode":"render_postgres" if USING_POSTGRES else "sqlite","storage":"render_postgres" if USING_POSTGRES else "local_temp","storage_path":"DATABASE_URL" if USING_POSTGRES else DB_PATH})
    except Exception:
        return jsonify({"ok":False,"service":"KETS","database_configured":False,"database_mode":"render_postgres" if USING_POSTGRES else "sqlite","storage":"render_postgres" if USING_POSTGRES else "local_temp","storage_path":"DATABASE_URL" if USING_POSTGRES else DB_PATH}),503

@app.route("/api/integration-status", methods=["GET"])
def api_integration_status():
    """Fast health check for the actual production connection path.

    Signal flow is intentionally one-way and local to this KETS service:
    trading bot -> POST /api/signals -> Signal History -> Auto-Trade -> cTrader.
    This endpoint never polls the bot or cTrader, so the dashboard cannot
    time out while checking connection status.
    """
    history = _history_items()
    latest = history[-1] if history else None
    return jsonify({
        "ok": True,
        "website": {"ok": True},
        "signal_feed": {
            "mode": "website-local-history",
            "configured": True,
            "history_count": len(history),
            "latest_signal_id": str((latest or {}).get("id") or ""),
            "latest_signal_time": (latest or {}).get("timestamp") or "",
        },
        "ctrader": {
            "app_credentials_configured": bool(_ctrader_client_id() and _ctrader_client_secret()),
            "auto_worker": bool(globals().get("autotrade_worker_started", False)),
        },
    })

@app.route("/api/diagnostics", methods=["GET"])
def api_diagnostics():
    try:
        with DB_LOCK:
            conn=db_conn(); conn.execute("SELECT 1").fetchone(); conn.close()
        return jsonify({"ok":True,"service":"KETS","database":{"ok":True,"stage":"render_postgres" if USING_POSTGRES else "sqlite","code":"DB_OK"},"storage_path":DB_PATH})
    except Exception:
        return jsonify({"ok":False,"service":"KETS","database":{"ok":False,"stage":"render_postgres" if USING_POSTGRES else "sqlite","code":"DB_UNAVAILABLE"}}),503

# Frontend routes. Render's web service must serve the KETS website itself.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route("/", methods=["GET"])
def index_page():
    return send_from_directory(BASE_DIR, "index.html")

@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    # Only serve known frontend assets; never expose Python/source files.
    allowed = {
        "styles.css", "app.js", "service-worker.js", "manifest.json",
        "icon-192.png", "icon-512.png", "kets-android.apk"
    }
    if filename not in allowed:
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(BASE_DIR, filename)

@app.route("/download/kets-android.apk", methods=["GET"])
def download_android():
    return send_from_directory(BASE_DIR, "kets-android.apk", as_attachment=True, download_name="KETS-Android.apk")


# ============================================================
# RENDER PERSISTENT STORAGE
# All website accounts, payments and received signals are stored
# in SQLite on the Render Persistent Disk. No Supabase database
# is used by the website.
# ============================================================
# ============================================================
# DATABASE STORAGE
# Render Postgres is used automatically when DATABASE_URL exists.
# SQLite remains as a local fallback for development.
# ============================================================
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    psycopg2 = None
    RealDictCursor = None
    POSTGRES_AVAILABLE = False

# KETS production storage is SQLite on the Render Persistent Disk.
# Deliberately do not auto-switch to PostgreSQL/Supabase from DATABASE_URL.
DATABASE_URL = ""
USING_POSTGRES = False

class PGConnection:
    def __init__(self, url):
        self.raw = psycopg2.connect(url, connect_timeout=10)
    def execute(self, sql, params=()):
        sql = _pg_sql(sql)
        cur = self.raw.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        return cur
    def executescript(self, sql):
        # Used only by init_db; split the static CREATE statements.
        cur = self.raw.cursor()
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(_pg_sql(statement))
        cur.close()
    def commit(self):
        self.raw.commit()
    def close(self):
        self.raw.close()
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        if exc_type:
            self.raw.rollback()
        else:
            self.raw.commit()
        self.raw.close()

def _pg_sql(sql):
    sql = sql.replace("COLLATE NOCASE", "")
    sql = sql.replace("INSERT OR REPLACE INTO", "INSERT INTO")
    # PostgreSQL accepts %s placeholders rather than SQLite's ?.
    sql = sql.replace("?", "%s")
    # For the two KETS tables that use INSERT OR REPLACE, emulate SQLite
    # replacement semantics with an upsert on the primary key.
    if sql.lstrip().upper().startswith("INSERT INTO"):
        m = re.search(r"INSERT INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)", sql, re.I | re.S)
        if m and "ON CONFLICT" not in sql.upper() and m.group(1).lower() in ("payments", "signals"):
            table=m.group(1)
            cols=[c.strip() for c in m.group(2).split(",")]
            updates=", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c.lower() != "id")
            sql += f" ON CONFLICT (id) DO UPDATE SET {updates}"
    return sql

def _choose_data_dir():
    configured = os.environ.get("KETS_DATA_DIR", "").strip()
    candidates = [configured] if configured else []
    candidates += ["/tmp/kets_data", os.path.join(BASE_DIR, "data")]
    for candidate in candidates:
        try:
            os.makedirs(candidate, exist_ok=True)
            return candidate
        except OSError:
            continue
    return "/tmp"

RENDER_DATA_DIR = _choose_data_dir()
DB_PATH = os.path.join(RENDER_DATA_DIR, "kets_website.db")

DB_LOCK = Lock()

def using_postgres():
    return USING_POSTGRES

def db_conn():
    if USING_POSTGRES:
        return PGConnection(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _db_sql(sql):
    return _pg_sql(sql) if USING_POSTGRES else sql

def db_execute(conn, sql, params=()):
    return conn.execute(_db_sql(sql), params)

def _init_core_db():
    if USING_POSTGRES:
        with db_conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                country_name TEXT,
                country_code TEXT NOT NULL,
                profile_picture TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS payments (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                tx_ref TEXT NOT NULL UNIQUE,
                tracking_id TEXT,
                plan TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL,
                network TEXT,
                email TEXT,
                phone TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_payments_user_status ON payments(user_id, status, updated_at);
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                direction TEXT,
                score DOUBLE PRECISION,
                timestamp TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
            CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT);
            """)
        print("✅ KETS Render Postgres storage enabled")
        return

    with db_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            country_name TEXT,
            country_code TEXT NOT NULL,
            profile_picture TEXT,
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
            currency TEXT NOT NULL,
            status TEXT NOT NULL,
            network TEXT,
            email TEXT,
            phone TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_payments_user_status ON payments(user_id, status, updated_at);
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            asset TEXT NOT NULL,
            direction TEXT,
            score REAL,
            timestamp TEXT,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_timestamp ON signals(timestamp);
        CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT);
        """)
    print(f"✅ KETS local SQLite fallback: {DB_PATH}")

COUNTRY_REQUIRED = True

# One database adapter is used by every route.  The previous build defined a
# second SQLite-only adapter later in this file, silently overriding the
# PostgreSQL adapter and causing production login/dashboard failures.
def using_postgres():
    return USING_POSTGRES

def db_conn():
    if USING_POSTGRES:
        return PGConnection(DATABASE_URL)
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _db_sql(sql):
    return _pg_sql(sql) if USING_POSTGRES else sql

def db_execute(conn, sql, params=()):
    return conn.execute(_db_sql(sql), params)

def _run_schema(conn, sql):
    if USING_POSTGRES:
        conn.executescript(sql)
    else:
        conn.executescript(sql)

def init_db():
    global DB_PATH
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    except OSError:
        DB_PATH = "/tmp/kets_website.db"
        os.makedirs("/tmp", exist_ok=True)

    # Core tables are created by the original, PostgreSQL-aware initializer.
    _init_core_db()

    # Tables used by cTrader and the managed-autotrading layer.  Keep this
    # schema compatible with both SQLite fallback and Render PostgreSQL.
    extra_schema = """
    CREATE TABLE IF NOT EXISTS managed_connectors (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        broker TEXT NOT NULL, mt5_login TEXT, mt5_server TEXT, status TEXT NOT NULL,
        auto_enabled INTEGER DEFAULT 0, balance DOUBLE PRECISION DEFAULT 0,
        equity DOUBLE PRECISION DEFAULT 0, free_margin DOUBLE PRECISION DEFAULT 0,
        positions_json TEXT DEFAULT '[]', last_signal_id TEXT, last_seen TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS managed_orders (
        id TEXT PRIMARY KEY, connector_id TEXT NOT NULL, signal_id TEXT NOT NULL,
        symbol TEXT NOT NULL, direction TEXT NOT NULL, volume DOUBLE PRECISION NOT NULL,
        entry DOUBLE PRECISION, take_profit DOUBLE PRECISION, stop_loss DOUBLE PRECISION,
        status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(connector_id, signal_id)
    );
    CREATE INDEX IF NOT EXISTS idx_managed_orders_connector_status
      ON managed_orders(connector_id, status);
    CREATE TABLE IF NOT EXISTS ctrader_connections (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE, broker TEXT,
        account_ids_json TEXT DEFAULT '[]', selected_account_id TEXT,
        access_token_enc TEXT NOT NULL, refresh_token_enc TEXT NOT NULL,
        expires_at TEXT, permission_scope TEXT, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_ctrader_connections_user
      ON ctrader_connections(user_id);
    """
    with DB_LOCK:
        conn = db_conn()
        try:
            _run_schema(conn, extra_schema)
            # Safe, idempotent upgrades for databases created by older builds.
            upgrades = {
                "managed_connectors": [
                    ("lot_size", "DOUBLE PRECISION DEFAULT 0.01"),
                    ("profit_target", "DOUBLE PRECISION DEFAULT 25"),
                    ("min_quality", "DOUBLE PRECISION DEFAULT 40"),
                    ("strong_only", "INTEGER DEFAULT 0"),
                    ("max_lot", "DOUBLE PRECISION DEFAULT 10.0"),
                    ("max_open_trades", "INTEGER DEFAULT 1"),
                    ("allocation_pct", "DOUBLE PRECISION DEFAULT 10"),
                ],
                "ctrader_connections": [
                    ("auto_enabled", "INTEGER DEFAULT 0"),
                    ("lot_size", "DOUBLE PRECISION DEFAULT 0.01"),
                    ("profit_target", "DOUBLE PRECISION DEFAULT 25"),
                    ("min_quality", "DOUBLE PRECISION DEFAULT 40"),
                    ("strong_only", "INTEGER DEFAULT 0"),
                    ("max_lot", "DOUBLE PRECISION DEFAULT 10.0"),
                    ("max_open_trades", "INTEGER DEFAULT 1"),
                    ("allocation_pct", "DOUBLE PRECISION DEFAULT 10"),
                    ("auto_symbol", "TEXT DEFAULT 'XAUUSD'"),
                    ("target_profit_total", "DOUBLE PRECISION DEFAULT 25"),
                    ("auto_direction", "TEXT"),
                    ("auto_cycle_start_equity", "DOUBLE PRECISION DEFAULT 0"),
                    ("auto_profit_total", "DOUBLE PRECISION DEFAULT 0"),
                    ("auto_entry_signal_id", "TEXT"),
                    ("balance", "DOUBLE PRECISION DEFAULT 0"),
                    ("equity", "DOUBLE PRECISION DEFAULT 0"),
                    ("free_margin", "DOUBLE PRECISION DEFAULT 0"),
                    ("positions_json", "TEXT DEFAULT '[]'"),
                    ("last_signal_id", "TEXT"),
                    ("last_error", "TEXT"),
                ],
            }
            for table, columns in upgrades.items():
                for col, definition in columns:
                    try:
                        db_execute(conn, f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
                    except Exception:
                        # Duplicate-column errors are expected on repeat boots.
                        pass
            conn.commit()
        finally:
            conn.close()
    print(f"✅ KETS persistent storage ready: {'Render PostgreSQL' if USING_POSTGRES else DB_PATH}")

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
        if payload.get("developer") is True:
            now = _now_iso()
            return {
                "id": "__kets_developer__",
                "email": os.environ.get("KETS_DEVELOPER_USERNAME", "developer"),
                "password_hash": "",
                "name": "KETS Developer",
                "country_name": "Uganda",
                "country_code": "UG",
                "profile_picture": "",
                "created_at": now,
                "updated_at": now,
                "role": "developer",
                "developer": True,
            }
        with DB_LOCK:
            conn = db_conn()
            row = db_execute(conn, "SELECT * FROM users WHERE id=?", (payload["user_id"],)).fetchone()
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None

# ============================================================
# cTRADER OPEN API CONNECTION
# OAuth callback + encrypted token storage.
# Existing KETS signal logic and MT5 connector remain untouched.
# ============================================================

CTRADER_AUTH_URL = "https://id.ctrader.com/my/settings/openapi/grantingaccess/"
CTRADER_TOKEN_URL = "https://openapi.ctrader.com/apps/token"
CTRADER_REDIRECT_DEFAULT = "https://kets.onrender.com/ctrader/callback"

def _ctrader_client_id():
    return os.environ.get("CTRADER_CLIENT_ID", "").strip()

def _ctrader_client_secret():
    return os.environ.get("CTRADER_CLIENT_SECRET", "").strip()

def _ctrader_redirect_uri():
    return os.environ.get("CTRADER_REDIRECT_URI", "").strip() or CTRADER_REDIRECT_DEFAULT

def _ctrader_fernet():
    raw = _payment_secret().encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))

def _ctrader_encrypt(value):
    return _ctrader_fernet().encrypt(str(value).encode("utf-8")).decode("ascii")

def _ctrader_oauth_serializer():
    return URLSafeTimedSerializer(_payment_secret(), salt="kets-ctrader-oauth-v1")

def _ctrader_make_state(user_id):
    return _ctrader_oauth_serializer().dumps({"user_id": str(user_id), "nonce": secrets.token_urlsafe(18)})

def _ctrader_read_state(state):
    return _ctrader_oauth_serializer().loads(state, max_age=600)

def _ctrader_row_for_user(user_id):
    with DB_LOCK:
        conn = db_conn()
        row = db_execute(conn, "SELECT * FROM ctrader_connections WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
    return dict(row) if row else None

CTRADER_SYMBOL_CACHE_LOCK = Lock()
CTRADER_SYMBOL_CACHE = {}
CTRADER_SYMBOL_CACHE_TTL = 900.0

# Short-lived account snapshot cache keeps the dashboard responsive without
# hammering cTrader every time the browser polls. Orders invalidate it.
CTRADER_SNAPSHOT_CACHE_LOCK = Lock()
CTRADER_SNAPSHOT_CACHE = {}
CTRADER_SNAPSHOT_CACHE_TTL = 10.0
CTRADER_SNAPSHOT_REFRESH_LOCK = Lock()
CTRADER_SNAPSHOT_REFRESH_IN_FLIGHT = set()

CTRADER_PAYLOAD = {
    "APP_AUTH_REQ": 2100, "ACCOUNT_AUTH_REQ": 2102,
    "NEW_ORDER_REQ": 2106, "CLOSE_POSITION_REQ": 2111,
    "ASSET_LIST_REQ": 2112, "SYMBOLS_LIST_REQ": 2114, "SYMBOL_BY_ID_REQ": 2116,
    "TRADER_REQ": 2121, "RECONCILE_REQ": 2124,
    "DEAL_LIST_REQ": 2133, "GET_ACCOUNT_LIST_REQ": 2149,
    "POSITION_PNL_REQ": 2187,
}

def _ctrader_host(is_live):
    return "wss://live.ctraderapi.com:5036" if bool(is_live) else "wss://demo.ctraderapi.com:5036"

async def _ctrader_json_call(access_token, is_live, requests_list):
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("The cTrader websocket dependency is not installed.") from exc
    async with websockets.connect(_ctrader_host(is_live), open_timeout=15, close_timeout=5, ping_interval=20, ping_timeout=20) as ws:
        async def send(payload_type, payload):
            msg={"clientMsgId":str(uuid.uuid4()),"payloadType":payload_type,"payload":payload}
            await ws.send(json.dumps(msg))
            for _ in range(12):
                raw=await asyncio.wait_for(ws.recv(), timeout=2.5)
                data=json.loads(raw)
                # Heartbeats/events may arrive between request and response.
                if data.get("payloadType") == 51:
                    continue
                return data
            raise RuntimeError("cTrader returned no response.")
        r=await send(CTRADER_PAYLOAD["APP_AUTH_REQ"], {"clientId":_ctrader_client_id(),"clientSecret":_ctrader_client_secret()})
        if r.get("payloadType") not in (2101,):
            raise RuntimeError((r.get("payload") or {}).get("description") or "cTrader application authorization failed.")
        out=[]
        for typ,payload in requests_list:
            out.append(await send(typ,payload))
        return out

def _ctrader_discover_accounts(access_token):
    accounts=[]
    for is_live in (False, True):
        try:
            responses=asyncio.run(_ctrader_json_call(access_token,is_live,[(CTRADER_PAYLOAD["GET_ACCOUNT_LIST_REQ"],{"accessToken":access_token})]))
            payload=(responses[0] or {}).get("payload") or {}
            for a in payload.get("ctidTraderAccount",[]) or []:
                aid=str(a.get("ctidTraderAccountId") or a.get("accountId") or "")
                if aid and not any(x["id"]==aid for x in accounts):
                    accounts.append({"id":aid,"is_live":bool(a.get("isLive",is_live)),"broker":a.get("brokerName") or a.get("broker") or "cTrader"})
        except Exception as exc:
            app.logger.info("cTrader %s account discovery unavailable: %s", "live" if is_live else "demo", exc)
    return accounts

def _ctrader_selected_account(row):
    try:
        accounts=json.loads(row.get("account_ids_json") or "[]")
    except Exception:
        accounts=[]
    selected=str(row.get("selected_account_id") or "")
    account=next((a for a in accounts if str(a.get("id"))==selected), None)
    if not account and accounts:
        account=accounts[0]
        selected=str(account.get("id"))
    if not account:
        raise RuntimeError("No cTrader account selected. Connect cTrader and choose an account first.")
    return account, selected

def _ctrader_refresh_token(row):
    refresh_enc=row.get("refresh_token_enc") or ""
    if not refresh_enc:
        raise RuntimeError("cTrader access token expired and no refresh token is stored. Reconnect cTrader.")
    refresh_token=_ctrader_fernet().decrypt(refresh_enc.encode()).decode()
    client_id=_ctrader_client_id(); client_secret=_ctrader_client_secret()
    if not client_id or not client_secret:
        raise RuntimeError("cTrader application credentials are not configured on the server.")
    response=requests.post(
        CTRADER_TOKEN_URL,
        params={"grant_type":"refresh_token","refresh_token":refresh_token,"client_id":client_id,"client_secret":client_secret},
        headers={"Accept":"application/json","Content-Type":"application/json"},
        timeout=15,
    )
    data=response.json()
    if response.status_code>=400 or not data.get("accessToken"):
        raise RuntimeError(data.get("description") or data.get("errorCode") or "cTrader token refresh failed. Reconnect cTrader.")
    access_token=data["accessToken"]
    new_refresh=data.get("refreshToken") or refresh_token
    expires_in=int(data.get("expiresIn") or 0)
    expires_at=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=expires_in)).isoformat() if expires_in else None
    with DB_LOCK:
        conn=db_conn()
        db_execute(conn,"UPDATE ctrader_connections SET access_token_enc=?,refresh_token_enc=?,expires_at=?,status=?,last_error=NULL,updated_at=? WHERE id=?",
                   (_ctrader_encrypt(access_token),_ctrader_encrypt(new_refresh),expires_at,"CONNECTED",_now_iso(),row["id"]))
        conn.commit(); conn.close()
    return access_token

def _ctrader_access_token(row, force_refresh=False):
    expires_at=row.get("expires_at")
    if not force_refresh and expires_at:
        try:
            if datetime.datetime.fromisoformat(str(expires_at).replace("Z","+00:00")) > datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=2):
                return _ctrader_fernet().decrypt(row["access_token_enc"].encode()).decode()
        except Exception:
            pass
    return _ctrader_refresh_token(row)

def _ctrader_snapshot_cached(row):
    key=(str(row.get("selected_account_id") or ""), str(row.get("user_id") or ""))
    now=time.time()
    with CTRADER_SNAPSHOT_CACHE_LOCK:
        item=CTRADER_SNAPSHOT_CACHE.get(key)
        if item and now-item["cached_at"] < CTRADER_SNAPSHOT_CACHE_TTL:
            return dict(item["snapshot"])
    return None

def _ctrader_cache_snapshot(row, snapshot):
    key=(str(row.get("selected_account_id") or ""), str(row.get("user_id") or ""))
    with CTRADER_SNAPSHOT_CACHE_LOCK:
        CTRADER_SNAPSHOT_CACHE[key]={"cached_at":time.time(),"snapshot":dict(snapshot)}

def _ctrader_invalidate_snapshot(row):
    key=(str(row.get("selected_account_id") or ""), str(row.get("user_id") or ""))
    with CTRADER_SNAPSHOT_CACHE_LOCK:
        CTRADER_SNAPSHOT_CACHE.pop(key, None)

def _ctrader_account_snapshot(row, retry=True):
    """Read the live cTrader account state used by the dashboard.

    Balance is the critical value: it comes directly from ProtoOATrader.
    Secondary requests (positions/PnL/deals/currency) are deliberately
    best-effort so a failure in one secondary endpoint can never turn a valid
    cTrader balance into the dashboard's fallback USD 0.
    """
    account, selected=_ctrader_selected_account(row)
    is_live=bool(account.get("is_live"))
    access_token=_ctrader_access_token(row)
    now_utc=datetime.datetime.now(datetime.timezone.utc)
    eat_now=now_utc.astimezone(EAT)
    eat_start=eat_now.replace(hour=0,minute=0,second=0,microsecond=0)
    from_ms=int(eat_start.astimezone(datetime.timezone.utc).timestamp()*1000)
    to_ms=int(now_utc.timestamp()*1000)

    async def run(token):
        import websockets
        async with websockets.connect(
            _ctrader_host(is_live),
            open_timeout=8,
            close_timeout=3,
            ping_interval=20,
            ping_timeout=10,
        ) as ws:

            async def send(pt,payload,expected=None,label="cTrader request",required=True):
                client_msg_id=str(uuid.uuid4())
                await ws.send(json.dumps({
                    "clientMsgId":client_msg_id,
                    "payloadType":pt,
                    "payload":payload
                }))
                for _ in range(12):
                    d=json.loads(await asyncio.wait_for(ws.recv(),timeout=2.5))
                    # Heartbeats/events can arrive on the same connection.
                    if d.get("payloadType")==51:
                        continue
                    # cTrader responses to our request should carry the same
                    # clientMsgId. Ignore unrelated asynchronous messages.
                    if d.get("clientMsgId") not in (None, client_msg_id):
                        continue
                    if d.get("payloadType")==2142:
                        ep=d.get("payload") or {}
                        msg=ep.get("description") or ep.get("errorCode") or f"{label} failed"
                        if required:
                            raise RuntimeError(msg)
                        return None, msg
                    if expected and d.get("payloadType")!=expected:
                        continue
                    return d, ""
                msg=f"cTrader did not return the expected {label} response"
                if required:
                    raise RuntimeError(msg)
                return None, msg

            # Application authentication and account authentication are required
            # before ProtoOATraderReq can read the selected account.
            await send(
                2100,
                {"clientId":_ctrader_client_id(),"clientSecret":_ctrader_client_secret()},
                2101,
                "application authorization",
                True,
            )
            await send(
                2102,
                {"ctidTraderAccountId":int(selected),"accessToken":token},
                2103,
                "account authorization",
                True,
            )

            # THIS is the authoritative balance request. Do it before any
            # optional requests and never discard it because another request
            # fails.
            trader_res,_=await send(
                2121,
                {"ctidTraderAccountId":int(selected)},
                2122,
                "account balance",
                True,
            )

            secondary = {}
            secondary_errors = []

            for key, pt, payload, expected, label in (
                ("reconcile",2124,
                 {"ctidTraderAccountId":int(selected),"returnProtectionOrders":False},
                 2125,"open positions"),
                ("pnl",2187,
                 {"ctidTraderAccountId":int(selected)},
                 2188,"open P/L"),
                ("deals",2133,
                 {"ctidTraderAccountId":int(selected),"fromTimestamp":from_ms,
                  "toTimestamp":to_ms,"maxRows":1000},
                 2134,"today's deals"),
                ("assets",2112,
                 {"ctidTraderAccountId":int(selected)},
                 2113,"account currency"),
            ):
                try:
                    res, err = await send(pt,payload,expected,label,False)
                    secondary[key]=res
                    if err:
                        secondary_errors.append(f"{label}: {err}")
                except Exception as exc:
                    secondary[key]=None
                    secondary_errors.append(f"{label}: {str(exc)[:180]}")

            return trader_res, secondary, secondary_errors

    try:
        trader_res, secondary, secondary_errors = asyncio.run(run(access_token))
    except RuntimeError as exc:
        msg=str(exc)
        if retry and any(x in msg.upper() for x in ("TOKEN", "AUTH", "INVALID", "ACCESS")):
            fresh_row=_ctrader_row_for_user(row["user_id"]) or row
            new_token=_ctrader_access_token(fresh_row,force_refresh=True)
            trader_res, secondary, secondary_errors = asyncio.run(run(new_token))
        else:
            raise

    trader_payload=(trader_res.get("payload") or {})
    # JSON Open API normally nests this as payload.trader. Accept the payload
    # itself as a fallback for broker/API variants without changing the normal path.
    trader=trader_payload.get("trader")
    if not isinstance(trader,dict):
        trader=trader_payload if isinstance(trader_payload,dict) else {}
    if not trader or trader.get("balance") is None:
        raise RuntimeError(
            "cTrader account authorization succeeded, but ProtoOATrader did not return a balance for the selected account."
        )

    money_digits=int(trader.get("moneyDigits") or 0)
    scale=10**money_digits
    raw_balance=trader.get("balance")
    balance=_num(raw_balance)/scale if scale else _num(raw_balance)

    # Secondary data is optional.
    recon_res=secondary.get("reconcile")
    recon_payload=(recon_res.get("payload") or {}) if isinstance(recon_res,dict) else {}
    raw_positions=recon_payload.get("position") or []

    pnl_res=secondary.get("pnl")
    pnl_payload=(pnl_res.get("payload") or {}) if isinstance(pnl_res,dict) else {}
    pnl_digits=int(
        pnl_payload.get("moneyDigits")
        if pnl_payload.get("moneyDigits") is not None
        else money_digits
    )
    pnl_scale=10**pnl_digits
    pnl_by_id={
        str(x.get("positionId")):_num(x.get("netUnrealizedPnL"))/pnl_scale
        for x in (pnl_payload.get("positionUnrealizedPnL") or [])
    }

    positions=[]
    used_margin=0.0
    unrealized=0.0
    for pos in raw_positions:
        td=pos.get("tradeData") or {}
        pid=str(pos.get("positionId") or "")
        net=pnl_by_id.get(pid,0.0)
        pdigits=int(pos.get("moneyDigits") or money_digits)
        pscale=10**pdigits
        margin=_num(pos.get("usedMargin"))/pscale if pscale else _num(pos.get("usedMargin"))
        used_margin+=margin
        unrealized+=net
        side=(
            "BUY" if int(td.get("tradeSide") or 0)==1
            else "SELL" if int(td.get("tradeSide") or 0)==2
            else "--"
        )
        positions.append({
            "positionId":pid,
            "symbolId":td.get("symbolId"),
            "side":side,
            "volume":_num(td.get("volume"))/100.0,
            "volume_cents":int(_num(td.get("volume"))),
            "entry_price":pos.get("price"),
            "stop_loss":pos.get("stopLoss"),
            "take_profit":pos.get("takeProfit"),
            "unrealized_pnl":net,
            "used_margin":margin,
            "open_timestamp":td.get("openTimestamp"),
            "label":td.get("label") or "",
            "comment":td.get("comment") or "",
        })

    realized_today=0.0
    deals_res=secondary.get("deals")
    deals=(deals_res.get("payload") or {}).get("deal") if isinstance(deals_res,dict) else []
    for deal in deals or []:
        if int(deal.get("dealStatus") or 0) not in (2,3):
            continue
        close=deal.get("closePositionDetail")
        if not isinstance(close,dict):
            continue
        digits=int(
            close.get("moneyDigits")
            if close.get("moneyDigits") is not None
            else deal.get("moneyDigits") or money_digits
        )
        div=10**digits
        gross=_num(close.get("grossProfit"))/div
        swap=_num(close.get("swap"))/div
        commission=_num(close.get("commission"))/div
        conversion=_num(close.get("pnlConversionFee"))/div
        realized_today += gross + swap + commission + conversion

    # ProtoOATrader is authoritative for balance. cTrader's ProtoOATrader
    # schema does not require equity/freeMargin fields, so use them only if a
    # broker/API variant supplies them; otherwise derive them.
    equity_raw=trader.get("equity")
    free_margin_raw=trader.get("freeMargin")
    equity=(
        _num(equity_raw)/scale
        if equity_raw is not None and scale
        else _num(equity_raw)
        if equity_raw is not None
        else balance+unrealized
    )
    free_margin=(
        _num(free_margin_raw)/scale
        if free_margin_raw is not None and scale
        else _num(free_margin_raw)
        if free_margin_raw is not None
        else equity-used_margin
    )

    assets_res=secondary.get("assets")
    assets=(assets_res.get("payload") or {}).get("asset") if isinstance(assets_res,dict) else []
    deposit_id=str(trader.get("depositAssetId") or "")
    currency=""
    for asset in assets or []:
        if str(asset.get("assetId"))==deposit_id:
            currency=str(
                asset.get("currency")
                or asset.get("code")
                or asset.get("name")
                or asset.get("displayName")
                or ""
            ).strip().upper()
            if len(currency)>5 and isinstance(asset.get("name"),str):
                m=re.search(r"\b([A-Z]{3})\b", currency)
                if m:
                    currency=m.group(1)
            break
    if not currency or currency=="NONE":
        # Most BlackBull demo accounts are USD, but prefer any currency field
        # returned directly by the broker/API if available.
        currency=str(
            trader.get("depositCurrency")
            or trader.get("currency")
            or "USD"
        ).strip().upper()
        if len(currency)>5:
            m=re.search(r"\b([A-Z]{3})\b",currency)
            if m:
                currency=m.group(1)

    # Keep the main dashboard usable even if optional data is temporarily
    # unavailable. This warning is surfaced separately and never replaces the
    # real balance.
    warning="; ".join(secondary_errors[:3])

    return {
        "selected_account_id":selected,
        "account_login":next(
            (a.get("login") or a.get("traderLogin") for a in [account] if a),
            None
        ),
        "account_type":"LIVE" if is_live else "DEMO",
        "currency":currency,
        "balance":balance,
        "equity":equity,
        "free_margin":free_margin,
        "today_pnl":realized_today,
        "realized_pnl":realized_today,
        "unrealized_pnl":unrealized,
        "used_margin":used_margin,
        "positions":positions,
        "server_time":_now_iso(),
        "data_warning":warning,
    }

@app.route("/api/ctrader/connect-url", methods=["GET"])
def ctrader_connect_url():
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in to KETS before connecting cTrader."}), 401
    client_id = _ctrader_client_id()
    if not client_id or not _ctrader_client_secret():
        return jsonify({"error": "cTrader is not configured yet. Add CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET in Render."}), 503
    from urllib.parse import urlencode
    state = _ctrader_make_state(user["id"])
    url = CTRADER_AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": _ctrader_redirect_uri(), "scope": "trading", "product": "web", "state": state})
    return jsonify({"url": url})

@app.route("/ctrader/connect", methods=["GET"])
def ctrader_connect():
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in to KETS before connecting cTrader."}), 401
    client_id = _ctrader_client_id()
    if not client_id:
        return jsonify({"error": "cTrader is not configured yet. Add CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET in Render."}), 503
    from urllib.parse import urlencode
    state = _ctrader_make_state(user["id"])
    url = CTRADER_AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": _ctrader_redirect_uri(),
        "scope": "trading",
        "product": "web",
        "state": state,
    })
    return redirect(url, code=302)

@app.route("/ctrader/callback", methods=["GET"])
def ctrader_callback():
    error = request.args.get("error", "").strip()
    if error:
        description = request.args.get("error_description", "cTrader authorization was cancelled.")
        return f"<h2>KETS cTrader connection</h2><p>{esc_html(description)}</p><p>You can close this page and try again from KETS.</p>", 400
    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    if not code or not state:
        return "<h2>KETS cTrader connection</h2><p>Missing authorization response. Start again from KETS.</p>", 400
    try:
        state_data = _ctrader_read_state(state)
        user_id = str(state_data["user_id"])
    except Exception:
        return "<h2>KETS cTrader connection</h2><p>The authorization session expired or was invalid. Start again from KETS.</p>", 400

    client_id = _ctrader_client_id()
    client_secret = _ctrader_client_secret()
    if not client_id or not client_secret:
        return "<h2>KETS cTrader connection</h2><p>KETS cTrader credentials are not configured on the server.</p>", 503

    try:
        response = requests.post(
            CTRADER_TOKEN_URL,
            params={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _ctrader_redirect_uri(),
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=15,
        )
        token_data = response.json()
    except Exception:
        return "<h2>KETS cTrader connection</h2><p>KETS could not contact cTrader to exchange the authorization code.</p>", 502

    if response.status_code >= 400 or not token_data.get("accessToken"):
        desc = token_data.get("description") or token_data.get("errorCode") or "cTrader rejected the authorization code."
        return f"<h2>KETS cTrader connection</h2><p>{esc_html(desc)}</p><p>Start the connection again from KETS.</p>", 400

    access_token = token_data["accessToken"]
    refresh_token = token_data.get("refreshToken") or ""
    expires_in = int(token_data.get("expiresIn") or 0)
    expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)).isoformat() if expires_in else None
    now = _now_iso()
    connection_id = str(uuid.uuid4())

    try:
        with DB_LOCK:
            conn = db_conn()
            db_execute(conn, "DELETE FROM ctrader_connections WHERE user_id=?", (user_id,))
            db_execute(conn, """INSERT INTO ctrader_connections
                (id,user_id,broker,account_ids_json,selected_account_id,access_token_enc,refresh_token_enc,expires_at,permission_scope,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (connection_id, user_id, "cTrader", "[]", None, _ctrader_encrypt(access_token), _ctrader_encrypt(refresh_token),
                 expires_at, "trading", "AUTHORIZED", now, now))
            conn.commit()
            conn.close()
    except Exception:
        return "<h2>KETS cTrader connection</h2><p>Authorization succeeded, but KETS could not save the connection.</p>", 500

    # Discover all accounts immediately so the user can choose demo or live.
    try:
        accounts = _ctrader_discover_accounts(access_token)
        with DB_LOCK:
            conn = db_conn()
            db_execute(conn, "UPDATE ctrader_connections SET account_ids_json=?, status=?, updated_at=? WHERE id=?",
                       (json.dumps(accounts), "CONNECTED" if accounts else "AUTHORIZED", _now_iso(), connection_id))
            conn.commit(); conn.close()
    except Exception as exc:
        app.logger.warning("cTrader account discovery deferred: %s", exc)
    base = os.environ.get("KETS_PUBLIC_BASE_URL", "").strip().rstrip("/") or "https://kets.onrender.com"
    return redirect(base + "/?ctrader=connected#managed-trading", code=302)

@app.route("/api/ctrader/status", methods=["GET"])
def ctrader_status():
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in required."}), 401
    row = _ctrader_row_for_user(user["id"])
    if not row:
        return jsonify({"connected": False, "status": "NOT_CONNECTED", "broker": "cTrader", "accounts": []})
    try:
        accounts = json.loads(row.get("account_ids_json") or "[]")
        if not isinstance(accounts, list):
            accounts = []
    except Exception:
        accounts = []
    return jsonify({
        "connected": row.get("status") in ("AUTHORIZED", "CONNECTED"),
        "status": row.get("status") or "AUTHORIZED",
        "broker": row.get("broker") or "cTrader",
        "accounts": accounts,
        "selected_account_id": row.get("selected_account_id"),
        "expires_at": row.get("expires_at"),
        "permission_scope": row.get("permission_scope") or "trading",
    })

@app.route("/api/ctrader/disconnect", methods=["POST"])
def ctrader_disconnect():
    user = _current_user()
    if not user:
        return jsonify({"error": "Sign in required."}), 401
    with DB_LOCK:
        conn = db_conn()
        db_execute(conn, "DELETE FROM ctrader_connections WHERE user_id=?", (user["id"],))
        conn.commit()
        conn.close()
    return jsonify({"ok": True, "connected": False, "status": "NOT_CONNECTED"})

def esc_html(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))

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

def _auth_json(payload, status=200):
    """Return an auth response and persist the login in a secure browser cookie.

    The cookie is needed for OAuth redirects (including cTrader), because an
    external redirect cannot carry the Authorization header from fetch().
    The bearer token remains supported for the web app and Android client.
    """
    response = make_response(jsonify(payload), status)
    token = payload.get("token") if isinstance(payload, dict) else None
    if token:
        response.set_cookie(
            "kets_user", token, max_age=30 * 24 * 3600, httponly=True,
            secure=request.is_secure, samesite="Lax", path="/"
        )
    return response

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
    try:
        with DB_LOCK:
            conn = db_conn()
            try:
                db_execute(conn,
                    "INSERT INTO users(id,email,password_hash,name,country_name,country_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (user_id,email,_hash_password(password),name,country_name,country_code,now,now)
                )
                conn.commit()
            except Exception as exc:
                if isinstance(exc, sqlite3.IntegrityError):
                    return jsonify({"error": "That email already has an account. Please sign in."}), 409
                raise
            finally:
                conn.close()
    except Exception as exc:
        app.logger.exception("KETS account creation failed")
        # Never expose database internals or a traceback to the browser.
        message = str(exc).lower()
        if "database is locked" in message:
            safe = "KETS account creation is temporarily busy. Please try again."
            code = "DB_LOCKED"
        elif "no such table" in message or "no such column" in message:
            safe = "KETS account storage is not initialized correctly on the Render persistent disk."
            code = "DB_SCHEMA_MISMATCH"
        else:
            safe = "KETS account creation is temporarily unavailable. Please try again shortly."
            code = "DB_UNAVAILABLE"
        return jsonify({"error": safe, "code": code}), 503
    return _auth_json({"ok": True, "token": _user_token(user_id), "user": _safe_user({"id":user_id,"email":email,"name":name,"country_name":country_name,"country_code":country_code,"profile_picture":"","created_at":now,"updated_at":now})})

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))

    # Developer accounts are deployment credentials, not rows in the public
    # users table. Check them before normal account lookup.
    eu = os.environ.get("KETS_DEVELOPER_USERNAME", "").strip()
    ep = os.environ.get("KETS_DEVELOPER_PASSWORD", "")
    if eu and ep and secrets.compare_digest(email, eu.lower()) and secrets.compare_digest(password, ep):
        token = _auth_serializer().dumps({"developer": True, "nonce": secrets.token_urlsafe(12)})
        developer = {
            "id": "__kets_developer__",
            "email": eu,
            "name": "KETS Developer",
            "country_name": "Uganda",
            "country_code": "UG",
            "profile_picture": "",
            "role": "developer",
            "developer": True,
        }
        return _auth_json({"ok": True, "token": token, "user": developer, "access": {"plan": "developer", "expires": None, "developer": True}})

    # Normal users are stored in SQLite on the Render Persistent Disk.
    try:
        with DB_LOCK:
            conn = db_conn()
            row = db_execute(conn, "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
            conn.close()
    except Exception as exc:
        app.logger.exception("Normal user sign-in database lookup failed")
        return jsonify({
            "error": "Sign-in service is temporarily unavailable. Please try again shortly.",
            "code": "DB_UNAVAILABLE",
        }), 503

    if not row:
        return jsonify({"error": "Account not found. Create a KETS account first."}), 401
    if not row["password_hash"] or not _check_password(password, row["password_hash"]):
        return jsonify({"error": "Email or password is incorrect."}), 401

    access = _user_access(row["id"])
    if not access:
        return jsonify({
            "error": "An active payment plan is required before you can sign in.",
            "payment_required": True,
            "plans_url": "/api/plans",
        }), 402
    return _auth_json({"ok": True, "token": _user_token(row["id"]), "user": _safe_user(row), "access": access})

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

# ============================================================
# cTRADER MANAGED TRADING
# All execution is performed through the user's authorized cTrader account.
# No Exness, MT5 terminal, broker password, deposit or withdrawal credentials.
# ============================================================
def _signal_age_seconds(sig):
    raw=sig.get("timestamp") or sig.get("time") or sig.get("datetime") or sig.get("created_at")
    if not raw: return None
    try:
        text=str(raw).replace("Z","+00:00")
        dt=datetime.datetime.fromisoformat(text)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=EAT)
        return max(0.0,(get_eat_time()-dt.astimezone(EAT)).total_seconds())
    except Exception: return None

def _current_signal(requested_symbol=None):
    signals=_latest_signal_map(_history_items())
    requested=str(requested_symbol or "").replace("/","").replace("_","").replace("-","").upper()
    if requested:
        if requested in {"XAUUSD","GOLD","XAU"}:
            for key in ("GOLD","XAUUSD","XAU"):
                if signals.get(key): return signals[key]
        elif requested in {"BTCUSD","BTC"}:
            for key in ("BTC","BTCUSD"):
                if signals.get(key): return signals[key]
        else:
            return signals.get(requested)
    market="GOLD" if get_eat_time().weekday()<5 else "BTC"
    return signals.get(market) or signals.get("XAUUSD" if market=="GOLD" else "BTCUSD")

def _signal_is_low_risk(sig, min_quality=40):
    values=[]
    for key in ("risk_level","risk","risk_classification","classification","entry_classification"):
        if sig.get(key) is not None: values.append(str(sig.get(key)).lower())
    text=" ".join(values+ [str(sig.get("interpretation") or "").lower(), str(sig.get("signal_type") or "").lower()])
    explicit_low=any(x in text for x in ("low risk","low-risk","very low risk","conservative"))
    explicit_high=any(x in text for x in ("high risk","high-risk","very high risk","no trade","avoid"))
    score=_num(sig.get("score") or sig.get("strength") or sig.get("signal_strength") or sig.get("confidence"))
    quality=_num(sig.get("entry_quality_score") or ((sig.get("entry_quality") or {}).get("score") if isinstance(sig.get("entry_quality"),dict) else None))
    return not explicit_high and (explicit_low or (not values and max(score,quality)>=min_quality))

def _ctrader_settings_row(user_id):
    row=_ctrader_row_for_user(user_id)
    return row

@app.route("/api/managed/connect", methods=["POST"])
def managed_connect():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    row=_ctrader_row_for_user(user["id"])
    if not row: return jsonify({"error":"Connect cTrader first using the official cTrader button."}),400
    return jsonify({"ok":True,"broker":"cTrader","status":row.get("status"),"connected":row.get("status") in ("AUTHORIZED","CONNECTED")})

@app.route("/api/managed/toggle", methods=["POST"])
def managed_toggle():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    body=request.get_json(silent=True) or {}; enabled=bool(body.get("enabled")); row=_ctrader_row_for_user(user["id"])
    if not row: return jsonify({"error":"Connect cTrader first."}),400
    if row.get("status") not in ("AUTHORIZED","CONNECTED"):
        return jsonify({"error":"cTrader is not connected."}),400
    if enabled and not row.get("selected_account_id"):
        return jsonify({"error":"Select a cTrader trading account before turning Auto-Trade ON."}),400
    baseline=_num(row.get("equity")) or _num(row.get("balance")) or 0.0
    with DB_LOCK:
        conn=db_conn()
        if enabled:
            db_execute(conn,"UPDATE ctrader_connections SET auto_enabled=1,last_error=NULL,auto_direction=NULL,auto_cycle_start_equity=?,auto_profit_total=0,auto_entry_signal_id=NULL,last_signal_id=NULL,updated_at=? WHERE id=?",(baseline,_now_iso(),row["id"]))
        else:
            db_execute(conn,"UPDATE ctrader_connections SET auto_enabled=0,last_error=NULL,auto_direction=NULL,auto_entry_signal_id=NULL,last_signal_id=NULL,updated_at=? WHERE id=?",(_now_iso(),row["id"]))
        conn.commit(); conn.close()
    # Do not make the browser wait for the broker. Kick the execution worker
    # immediately; the worker also performs an atomic signal claim so multiple
    # web workers cannot submit the same signal twice.
    if enabled:
        Thread(target=_ctrader_autotrade_once, daemon=True, name="kets-ctrader-immediate-check").start()
    return jsonify({"ok":True,"auto_enabled":enabled})

@app.route("/api/managed/status")
def managed_status():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    row=_ctrader_row_for_user(user["id"])
    if not row: return jsonify({"connected":False,"status":"NOT_CONNECTED","broker":"cTrader","positions":[]})
    connected=row.get("status") in ("AUTHORIZED","CONNECTED")
    snapshot=_ctrader_snapshot_cached(row) if connected and row.get("selected_account_id") else None
    live_error=row.get("last_error") or ""
    # IMPORTANT: never make the dashboard request wait for a full cTrader
    # websocket/account reconciliation. Manual trading can remain fast while
    # the status endpoint responds immediately from the last known state.
    # A background refresh updates balance/positions without blocking the web UI.
    if connected and row.get("selected_account_id") and snapshot is None:
        snapshot={
            "balance":_num(row.get("balance")),
            "equity":_num(row.get("equity")),
            "free_margin":_num(row.get("free_margin")),
            "positions":positions if "positions" in locals() else [],
            "today_pnl":0, "realized_pnl":0, "unrealized_pnl":0,
            "currency":"USD", "server_time":_now_iso()
        }
    if connected and row.get("selected_account_id"):
        def _refresh_status_async(r, uid):
            key=(str(r.get("selected_account_id") or ""), str(r.get("user_id") or ""))
            with CTRADER_SNAPSHOT_REFRESH_LOCK:
                if key in CTRADER_SNAPSHOT_REFRESH_IN_FLIGHT:
                    return
                CTRADER_SNAPSHOT_REFRESH_IN_FLIGHT.add(key)
            try:
                snap=_ctrader_account_snapshot(r)
                _ctrader_cache_snapshot(r,snap)
                with DB_LOCK:
                    conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET balance=?,equity=?,free_margin=?,positions_json=?,last_error=NULL,status=?,updated_at=? WHERE id=?",
                        (snap.get("balance",0),snap.get("equity",0),snap.get("free_margin",0),json.dumps(snap.get("positions",[])),"CONNECTED",_now_iso(),r["id"])); conn.commit(); conn.close()
            except Exception as exc:
                msg=str(exc)[:500]
                with DB_LOCK:
                    conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET last_error=?,updated_at=? WHERE id=?",(msg,_now_iso(),r["id"])); conn.commit(); conn.close()
            finally:
                with CTRADER_SNAPSHOT_REFRESH_LOCK:
                    CTRADER_SNAPSHOT_REFRESH_IN_FLIGHT.discard(key)
        Thread(target=_refresh_status_async,args=(dict(row),user["id"]),daemon=True,name="kets-ctrader-status-refresh").start()
    try: positions=json.loads((row.get("positions_json") or "[]"))
    except Exception: positions=[]
    return jsonify({
        "connected":connected,
        "status":row.get("status") or "NOT_CONNECTED",
        "auto_enabled":bool(row.get("auto_enabled")),
        "auto_symbol":str(row.get("auto_symbol") or "XAUUSD").upper(),
        "broker":"cTrader",
        "selected_account_id":row.get("selected_account_id"),
        "currency":(snapshot or {}).get("currency") or "USD",
        "balance":(snapshot or {}).get("balance",row.get("balance",0)),
        "equity":(snapshot or {}).get("equity",row.get("equity",0)),
        "free_margin":(snapshot or {}).get("free_margin",row.get("free_margin",0)),
        "today_pnl":(snapshot or {}).get("today_pnl",0),
        "realized_pnl":(snapshot or {}).get("realized_pnl",0),
        "unrealized_pnl":(snapshot or {}).get("unrealized_pnl",0),
        "positions":(snapshot or {}).get("positions",positions),
        "last_error":live_error or row.get("last_error") or "",
        "data_warning":(snapshot or {}).get("data_warning") or "",
        "server_time":(snapshot or {}).get("server_time") or _now_iso(),
        "settings":{
            "lot_size":_num(row.get("lot_size")) or .01,
            "profit_target":_num(row.get("target_profit_total")) if row.get("target_profit_total") is not None else (_num(row.get("profit_target")) if row.get("profit_target") is not None else 25),
            "target_profit_total":_num(row.get("target_profit_total")) if row.get("target_profit_total") is not None else (_num(row.get("profit_target")) if row.get("profit_target") is not None else 25),
            "auto_direction":str(row.get("auto_direction") or ""),
            "auto_profit_total":_num(row.get("auto_profit_total")),
            "min_quality":_num(row.get("min_quality")) if row.get("min_quality") is not None else 40,
            "strong_only":bool(row.get("strong_only")),
            "max_lot":_num(row.get("max_lot")) or 10,
            "max_open_trades":int(row.get("max_open_trades") or 1),
            "allocation_pct":_num(row.get("allocation_pct")) or 10,
        },
    })

@app.route("/api/ctrader/select-account", methods=["POST"])
def ctrader_select_account():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    row=_ctrader_row_for_user(user["id"]); body=request.get_json(silent=True) or {}; selected=str(body.get("account_id") or "")
    if not row: return jsonify({"error":"Connect cTrader first."}),400
    accounts=json.loads(row.get("account_ids_json") or "[]")
    if not any(str(a.get("id"))==selected for a in accounts): return jsonify({"error":"That cTrader account is not available."}),400
    with DB_LOCK:
        conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET selected_account_id=?,status=?,updated_at=? WHERE id=?",(selected,"CONNECTED",_now_iso(),row["id"])); conn.commit(); conn.close()
    return jsonify({"ok":True,"selected_account_id":selected})

@app.route("/api/managed/settings", methods=["GET","POST"])
def managed_settings():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    row=_ctrader_row_for_user(user["id"])
    if not row: return jsonify({"error":"Connect cTrader first."}),400
    if request.method=="POST":
        body=request.get_json(silent=True) or {}
        try: lot=max(0.01,min(float(body.get("lot_size",row.get("lot_size") or .01)),10))
        except Exception: lot=.01
        try: target=max(0,min(float(body.get("target_profit_total",body.get("profit_target",row.get("target_profit_total") or row.get("profit_target") or 25))),100000))
        except Exception: target=25
        try: quality=max(0,min(float(body.get("min_quality",row.get("min_quality") if row.get("min_quality") is not None else 40)),100))
        except Exception: quality=40
        strong=1 if bool(body.get("strong_only",row.get("strong_only") or 0)) else 0
        auto_symbol=str(body.get("auto_symbol",row.get("auto_symbol") or "XAUUSD")).replace("/","").replace("_","").replace("-","").upper()
        if auto_symbol not in {"XAUUSD","BTCUSD"}: auto_symbol="XAUUSD"
        try: max_lot=max(lot,min(float(body.get("max_lot",row.get("max_lot") or 10)),10))
        except Exception: max_lot=max(lot,10)
        try: max_open=max(1,min(int(body.get("max_open_trades",row.get("max_open_trades") or 1)),10))
        except Exception: max_open=1
        try: allocation=max(1,min(float(body.get("allocation_pct",row.get("allocation_pct") or 10)),100))
        except Exception: allocation=10
        with DB_LOCK:
            conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET lot_size=?,profit_target=?,target_profit_total=?,min_quality=?,strong_only=?,max_lot=?,max_open_trades=?,allocation_pct=?,auto_symbol=?,updated_at=? WHERE id=?",(lot,target,target,quality,strong,max_lot,max_open,allocation,auto_symbol,_now_iso(),row["id"])); conn.commit(); conn.close()
        row=_ctrader_row_for_user(user["id"])
    return jsonify({"ok":True,"lot_size":_num(row.get("lot_size")) or .01,"profit_target":_num(row.get("target_profit_total")) if row.get("target_profit_total") is not None else (_num(row.get("profit_target")) if row.get("profit_target") is not None else 25),"target_profit_total":_num(row.get("target_profit_total")) if row.get("target_profit_total") is not None else (_num(row.get("profit_target")) if row.get("profit_target") is not None else 25),"min_quality":_num(row.get("min_quality")) if row.get("min_quality") is not None else 40,"strong_only":bool(row.get("strong_only")),"max_lot":_num(row.get("max_lot")) or 10,"max_open_trades":int(row.get("max_open_trades") or 1),"allocation_pct":_num(row.get("allocation_pct")) or 10,"auto_symbol":str(row.get("auto_symbol") or "XAUUSD").upper()})

def _ctrader_symbol_for_order(symbols, requested):
    """Resolve the broker-specific cTrader symbol ID.

    cTrader symbol IDs are broker/server specific, so KETS must discover the
    symbol on the selected account instead of assuming a universal ID.
    """
    req = str(requested or "").replace("/", "").replace("_", "").replace("-", "").upper()
    aliases = {
        "XAUUSD": {"XAUUSD", "GOLD", "XAUUS D".replace(" ", "")},
        "BTCUSD": {"BTCUSD", "BTCUSD."},
    }
    candidates = aliases.get(req, {req})
    exact = []
    for s in symbols or []:
        name = str(s.get("symbolName") or s.get("name") or "")
        norm = name.replace("/", "").replace("_", "").replace("-", "").replace(".", "").upper()
        if norm in candidates or norm == req:
            exact.append(s)
    if not exact:
        # Be tolerant of broker suffixes such as XAUUSDm / BTCUSD.r.
        for s in symbols or []:
            name = str(s.get("symbolName") or s.get("name") or "")
            norm = name.replace("/", "").replace("_", "").replace("-", "").replace(".", "").upper()
            if norm.startswith(req) or req.startswith(norm):
                exact.append(s)
    if not exact:
        raise RuntimeError(
            f"cTrader does not expose a tradable {requested} symbol on the selected account."
        )
    # Prefer enabled symbols and the shortest broker suffix.
    exact.sort(key=lambda s: (not bool(s.get("enabled", True)),
                              len(str(s.get("symbolName") or ""))))
    return exact[0]


def _ctrader_cached_symbol(account_id, requested_symbol):
    key=(str(account_id), str(requested_symbol).upper())
    now=time.time()
    with CTRADER_SYMBOL_CACHE_LOCK:
        item=CTRADER_SYMBOL_CACHE.get(key)
        if item and now-item["cached_at"] < CTRADER_SYMBOL_CACHE_TTL:
            return dict(item["symbol"])
    return None

def _ctrader_cache_symbol(account_id, requested_symbol, symbol):
    key=(str(account_id), str(requested_symbol).upper())
    with CTRADER_SYMBOL_CACHE_LOCK:
        CTRADER_SYMBOL_CACHE[key]={"cached_at":time.time(),"symbol":dict(symbol)}

def _ctrader_execute_order(row, order, auto_label=False):
    """Execute a real market order through the user's selected cTrader account.

    KETS volume fields are displayed as lots (0.01, 0.02, ...). cTrader's
    Open API expects volume in cents of the symbol's base units, so the
    broker-provided lotSize is used rather than assuming every CFD/FX symbol
    has the same contract size.
    """
    account, selected = _ctrader_selected_account(row)
    is_live = bool(account.get("is_live"))
    token = _ctrader_access_token(row)

    requested_symbol = str(order.get("symbol") or "XAUUSD").upper()
    direction = str(order.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        raise RuntimeError("Choose BUY or SELL.")
    try:
        lots = float(order.get("volume") or 0.01)
    except Exception:
        lots = 0.01
    if not math.isfinite(lots) or lots <= 0:
        raise RuntimeError("Lot size must be greater than 0.")

    async def run(access_token):
        import websockets

        async with websockets.connect(
            _ctrader_host(is_live),
            open_timeout=15,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            async def send(pt, payload, expected=None, label="cTrader request"):
                client_msg_id = str(uuid.uuid4())
                await ws.send(json.dumps({
                    "clientMsgId": client_msg_id,
                    "payloadType": pt,
                    "payload": payload,
                }))
                for _ in range(12):
                    d = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.5))
                    if d.get("payloadType") == 51:
                        continue
                    if d.get("clientMsgId") not in (None, client_msg_id):
                        continue
                    payload_type = d.get("payloadType")
                    # cTrader order validation failures are reported as
                    # ProtoOAOrderErrorEvent (2132), not only generic 2142.
                    if payload_type in (2132, 2142):
                        ep = d.get("payload") or {}
                        raise RuntimeError(
                            ep.get("description")
                            or ep.get("errorCode")
                            or f"{label} failed"
                        )
                    # New-order/close requests return ProtoOAExecutionEvent
                    # (2126). Surface rejected/cancel-rejected execution events.
                    if payload_type == 2126:
                        ep = d.get("payload") or {}
                        execution_type = int(ep.get("executionType") or 0)
                        if execution_type in (7, 8):
                            raise RuntimeError(
                                ep.get("description")
                                or f"cTrader rejected the {label}."
                            )
                    if expected and payload_type != expected:
                        continue
                    return d
                raise RuntimeError(f"cTrader did not return the expected {label} response.")

            await send(
                CTRADER_PAYLOAD["APP_AUTH_REQ"],
                {"clientId": _ctrader_client_id(), "clientSecret": _ctrader_client_secret()},
                2101,
                "application authorization",
            )
            await send(
                CTRADER_PAYLOAD["ACCOUNT_AUTH_REQ"],
                {"ctidTraderAccountId": int(selected), "accessToken": access_token},
                2103,
                "account authorization",
            )

            # Symbol IDs and contract sizes are broker/server specific. Cache
            # the resolved full symbol briefly so every order does not spend two
            # extra round trips fetching the same metadata.
            symbol = _ctrader_cached_symbol(selected, requested_symbol)
            if symbol is None:
                symbols_res = await send(
                    CTRADER_PAYLOAD["SYMBOLS_LIST_REQ"],
                    {"ctidTraderAccountId": int(selected)},
                    2115,
                    "symbol list",
                )
                payload = symbols_res.get("payload") or {}
                symbols = payload.get("symbol") or []
                symbol = _ctrader_symbol_for_order(symbols, requested_symbol)
                symbol_id = int(symbol.get("symbolId"))
                if not bool(symbol.get("enabled", True)):
                    raise RuntimeError(f"{symbol.get('symbolName') or requested_symbol} is disabled for trading.")
                symbol_details_res = await send(
                    CTRADER_PAYLOAD["SYMBOL_BY_ID_REQ"],
                    {"ctidTraderAccountId": int(selected), "symbolId": [symbol_id]},
                    2117,
                    "full symbol details",
                )
                full_symbols = (symbol_details_res.get("payload") or {}).get("symbol") or []
                full_symbol = next(
                    (s for s in full_symbols if int(s.get("symbolId") or -1) == symbol_id),
                    None,
                )
                if not full_symbol:
                    raise RuntimeError(
                        f"cTrader did not return full symbol details for {symbol.get('symbolName') or requested_symbol}."
                    )
                symbol = {**symbol, **full_symbol}
                _ctrader_cache_symbol(selected, requested_symbol, symbol)
            symbol_id = int(symbol.get("symbolId"))
            if not bool(symbol.get("enabled", True)):
                raise RuntimeError(f"{symbol.get('symbolName') or requested_symbol} is disabled for trading.")

            lot_size_cents = int(_num(symbol.get("lotSize")) or 0)
            min_volume = int(_num(symbol.get("minVolume")) or 0)
            max_volume = int(_num(symbol.get("maxVolume")) or 0)
            step_volume = int(_num(symbol.get("stepVolume")) or 0)
            if lot_size_cents <= 0:
                # A broker must normally provide lotSize. Do not guess a
                # contract size for gold/CFDs because that could create the
                # wrong position size.
                raise RuntimeError(
                    f"cTrader did not provide the contract lot size for {symbol.get('symbolName') or requested_symbol}."
                )

            # 1 website lot = the broker's cTrader lotSize.
            requested_volume = int(round(lots * lot_size_cents))
            volume = requested_volume

            if step_volume > 0 and volume % step_volume:
                # Never silently change the trader's requested lot size.
                # Reject values that the broker cannot represent exactly.
                step_lots = step_volume / lot_size_cents
                raise RuntimeError(
                    f"Lot size {lots:g} does not match the broker volume step "
                    f"({step_lots:g} lot) for {symbol.get('symbolName') or requested_symbol}."
                )

            if min_volume > 0 and volume < min_volume:
                raise RuntimeError(
                    f"Selected lot size {lots:g} is below the broker minimum "
                    f"({min_volume / lot_size_cents:g} lot) for {symbol.get('symbolName') or requested_symbol}."
                )
            if max_volume > 0 and volume > max_volume:
                raise RuntimeError(
                    f"Lot size {lots:g} exceeds the broker's maximum volume for {symbol.get('symbolName') or requested_symbol}."
                )
            if volume <= 0:
                raise RuntimeError("The selected lot size is below the broker's minimum trade volume.")

            trade_side = 1 if direction == "BUY" else 2
            payload = {
                "ctidTraderAccountId": int(selected),
                "symbolId": symbol_id,
                "orderType": 1,       # MARKET
                "tradeSide": trade_side,
                "volume": volume,
                "label": "KETS_AUTO" if auto_label else "KETS",
                "comment": "KETS automatic signal trade" if auto_label else "KETS manual trade",
            }

            sl = order.get("stop_loss")
            tp = order.get("take_profit")
            if sl not in (None, "", 0):
                payload["stopLoss"] = float(sl)
            if tp not in (None, "", 0):
                payload["takeProfit"] = float(tp)

            response = await send(
                CTRADER_PAYLOAD["NEW_ORDER_REQ"],
                payload,
                2126,
                "market order",
            )
            rp = response.get("payload") or {}
            # A successful cTrader response can contain an order/position/deal
            # depending on broker execution timing. Return the useful details.
            return {
                "symbol": symbol.get("symbolName") or requested_symbol,
                "symbol_id": symbol_id,
                "direction": direction,
                "lots": lots,
                "volume_cents": volume,
                "broker_lot_size": lot_size_cents,
                "broker_min_volume": min_volume,
                "broker_max_volume": max_volume,
                "broker_step_volume": step_volume,
                "order_id": rp.get("orderId") or (rp.get("order") or {}).get("orderId"),
                "position_id": rp.get("positionId") or (rp.get("position") or {}).get("positionId"),
                "execution_type": rp.get("executionType"),
                "execution_price": rp.get("executionPrice") or (rp.get("deal") or {}).get("executionPrice"),
                "raw": rp,
            }

    try:
        return asyncio.run(run(token)), selected
    except RuntimeError as exc:
        msg = str(exc)
        if any(x in msg.upper() for x in ("TOKEN", "AUTH", "ACCESS", "EXPIRED")):
            fresh_row = _ctrader_row_for_user(row["user_id"]) or row
            fresh_token = _ctrader_access_token(fresh_row, force_refresh=True)
            return asyncio.run(run(fresh_token)), selected
        raise


def _signal_is_strong_reversal(sig):
    """Return True only for signals explicitly classified by KETS as strong reversals."""
    if not isinstance(sig, dict):
        return False
    raw = sig.get("strong_reversal", sig.get("reversal_signal", False))
    if raw is True or str(raw).lower() == "true":
        return True
    text = " ".join(
        str(sig.get(k) or "")
        for k in ("signal_type", "classification", "setup")
    ).upper()
    return "STRONG REVERSAL" in text


def _signal_execution_ready(sig, allow_history=False):
    """Return True only for a complete BUY/SELL signal in READY state.

    The live Current KETS card is intentionally time-limited, but Auto-Trade
    may also consume a READY signal that came from the trading-bot feed and is
    visible in Signal History.  That lets the execution worker keep working
    even when the Current KETS endpoint/card is temporarily unavailable.
    """
    if not isinstance(sig, dict):
        return False
    direction=str(sig.get("direction") or sig.get("signal") or "").upper()
    if direction not in {"BUY","SELL"}:
        return False
    if not allow_history:
        # Current KETS is a live plan, so reject an excessively old live copy.
        age=_signal_age_seconds(sig)
        if age is not None and age > 180:
            return False
    # The existing trading bot sends qualifying signals with status=ACTIVE.
    # For website auto-trading, a complete bot signal is execution-ready even
    # when its display status remains ACTIVE. Prefer the explicit execution
    # status when present so the website can keep display and execution state
    # separate.
    execution_status=str(sig.get("execution_status") or sig.get("entry_exit_status") or "").strip().upper()
    if execution_status:
        return execution_status == "READY"
    status=str(sig.get("status") or "").strip().upper()
    if status:
        if status == "READY":
            return True
        if status == "ACTIVE":
            entry=_num(sig.get("market_price") or sig.get("price") or sig.get("entry"))
            tp=_num(sig.get("take_profit") or sig.get("tp") or sig.get("target"))
            sl=_num(sig.get("stop_loss") or sig.get("sl") or sig.get("stop"))
            return bool(entry and tp and sl)
        return False
    entry=_num(sig.get("market_price") or sig.get("price") or sig.get("entry"))
    tp=_num(sig.get("take_profit") or sig.get("tp") or sig.get("target"))
    sl=_num(sig.get("stop_loss") or sig.get("sl") or sig.get("stop"))
    return bool(entry and tp and sl)


def _signal_matches_symbol(sig, requested_symbol):
    requested=str(requested_symbol or "").replace("/","").replace("_","").replace("-","").upper()
    asset=str((sig or {}).get("asset") or (sig or {}).get("market") or "").replace("/","").replace("_","").replace("-","").upper()
    if requested in {"XAUUSD","GOLD","XAU"}:
        return asset in {"XAUUSD","GOLD","XAU"}
    if requested in {"BTCUSD","BTC"}:
        return asset in {"BTCUSD","BTC"}
    return asset == requested


def _latest_ready_history_signal(requested_symbol=None):
    """Get the newest READY trading-bot signal visible in Signal History.

    This is deliberately separate from _current_signal(): the latter powers
    the live dashboard and can require a very recent signal, while Auto-Trade
    needs the trading-bot history as a resilient execution source.
    """
    candidates=[]
    for sig in _history_items():
        if not _signal_matches_symbol(sig, requested_symbol):
            continue
        if _signal_execution_ready(sig, allow_history=True):
            candidates.append(sig)
    if not candidates:
        return None
    def stamp(sig):
        raw=sig.get("timestamp") or sig.get("timestamp_utc") or sig.get("created_at")
        try:
            dt=datetime.datetime.fromisoformat(str(raw).replace("Z","+00:00"))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=EAT)
            return dt.timestamp()
        except Exception:
            return 0.0
    return max(candidates, key=stamp)


def _signal_id(sig):
    return str(sig.get("id") or (str(sig.get("asset"))+"-"+str(sig.get("timestamp"))))


def _auto_profit_progress(row):
    """Profit progress for the current Auto-Trade run.

    It is measured from the account equity captured when Auto-Trade was turned
    on, so both realized and open P/L contribute to the target total.
    """
    start=_num(row.get("auto_cycle_start_equity"))
    current=_num(row.get("equity")) or _num(row.get("balance"))
    if start <= 0 or current <= 0:
        return _num(row.get("auto_profit_total"))
    return current-start


def _queue_and_execute_ctrader(row, sig, automatic=True):
    direction=str(sig.get("direction") or sig.get("signal") or "").upper()
    if direction not in {"BUY","SELL"}: raise RuntimeError("Signal is not a BUY/SELL entry.")
    entry=_num(sig.get("market_price") or sig.get("price") or sig.get("entry"))
    if not entry: raise RuntimeError("The current KETS signal has no entry price.")
    sid=_signal_id(sig)
    lot=max(.01,min(_num(row.get("lot_size")) or .01,_num(row.get("max_lot")) or 10))
    # Automatic trading follows Current KETS signal direction only. Its exit is
    # the next opposite KETS signal, so do not attach the signal TP/SL to auto
    # positions. Manual orders keep their optional SL/TP behavior unchanged.
    order={"symbol":str(row.get("auto_symbol") or sig.get("symbol") or ("XAUUSD" if str(sig.get("asset")).upper() in ("GOLD","XAUUSD") else "BTCUSD")),"direction":direction,"volume":lot,"entry":entry}
    if not automatic:
        tp=_num(sig.get("take_profit") or sig.get("tp") or sig.get("target"))
        sl=_num(sig.get("stop_loss") or sig.get("sl") or sig.get("stop"))
        if not tp or not sl: raise RuntimeError("The current signal has no complete entry, target and stop-loss plan.")
        order["take_profit"]=tp; order["stop_loss"]=sl
    result,selected=_ctrader_execute_order(row,order,auto_label=automatic)
    with DB_LOCK:
        conn=db_conn()
        db_execute(conn,"UPDATE ctrader_connections SET selected_account_id=?,last_signal_id=?,auto_entry_signal_id=?,auto_direction=?,last_error=NULL,status=?,updated_at=? WHERE id=?",(selected,sid,sid,direction,"CONNECTED",_now_iso(),row["id"]))
        conn.commit(); conn.close()
    _ctrader_invalidate_snapshot(row)
    return result


def _ctrader_autotrade_once():
    # Auto-Trade is strictly limited to 06:00-18:00 EAT. Outside this window
    # the worker remains alive but performs no automatic entries or exits.
    # It resumes automatically at 06:00 EAT and continues the same target
    # profit cycle until the configured total target is reached.
    if not trading_hours_open():
        return
    with DB_LOCK:
        conn=db_conn(); rows=db_execute(conn,"SELECT * FROM ctrader_connections WHERE auto_enabled=1 AND status IN ('AUTHORIZED','CONNECTED')").fetchall(); conn.close()
    if not rows: return
    for rr in rows:
        row=dict(rr)
        try:
            auto_symbol=str(row.get("auto_symbol") or "XAUUSD").upper()
            # Auto-Trade uses only the trading-bot signals already stored in this
            # website's Signal History. No external signal URL is polled.
            # The entry/exit
            # rules do not change: only READY BUY/SELL signals are actionable,
            # repeated same-direction signals are ignored, and an opposite
            # signal closes the existing automatic position before the new one.
            sig=_latest_ready_history_signal(auto_symbol)
            if not sig:
                continue
            sid=_signal_id(sig)
            direction=str(sig.get("direction") or sig.get("signal") or "").upper()

            # Refresh account equity only when the cached row does not yet have
            # a baseline. The dashboard itself refreshes these fields every few
            # seconds, keeping the hot signal loop fast.
            if _num(row.get("auto_cycle_start_equity")) <= 0:
                try:
                    snap=_ctrader_account_snapshot(row)
                    baseline=_num(snap.get("equity")) or _num(snap.get("balance"))
                    with DB_LOCK:
                        conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET auto_cycle_start_equity=?,equity=?,balance=?,free_margin=?,positions_json=?,auto_profit_total=?,updated_at=? WHERE id=?",(baseline,snap.get("equity",0),snap.get("balance",0),snap.get("free_margin",0),json.dumps(snap.get("positions",[])),0,_now_iso(),row["id"])); conn.commit(); conn.close()
                    row["auto_cycle_start_equity"]=baseline; row["equity"]=snap.get("equity",0); row["balance"]=snap.get("balance",0)
                except Exception as snap_exc:
                    app.logger.warning("cTrader auto-trade baseline refresh failed: %s",snap_exc)

            previous_direction=str(row.get("auto_direction") or "").upper()
            previous_sid=str(row.get("auto_entry_signal_id") or "")

            # A signal change is the ONLY automatic exit condition. Close KETS
            # auto positions before entering the new direction.
            if previous_direction and direction != previous_direction:
                _ctrader_close_positions(row, auto_only=True)
                with DB_LOCK:
                    conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET auto_direction=NULL,auto_entry_signal_id=NULL,last_signal_id=NULL,last_error=NULL,updated_at=? WHERE id=?",(_now_iso(),row["id"])); conn.commit(); conn.close()
                previous_direction=""
                previous_sid=""

            progress=_auto_profit_progress(row)
            target=_num(row.get("target_profit_total")) if row.get("target_profit_total") is not None else (_num(row.get("profit_target")) or 25)
            with DB_LOCK:
                conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET auto_profit_total=?,updated_at=? WHERE id=?",(progress,_now_iso(),row["id"])); conn.commit(); conn.close()
            if target > 0 and progress >= target:
                # Target total reached: stop NEW automatic entries. Existing
                # auto positions remain open until the KETS direction changes.
                app.logger.info("cTrader auto-trade target total reached: %.2f / %.2f",progress,target)
                continue

            # Current KETS rule: one automatic entry for the active direction.
            # Repeated same-direction signals do not open duplicate positions.
            # Only an opposite READY signal closes the active auto position and
            # starts the new direction.
            if direction == previous_direction:
                continue

            claim=f"__EXECUTING__:{sid}"
            with DB_LOCK:
                conn=db_conn()
                cur=db_execute(conn,"UPDATE ctrader_connections SET last_signal_id=? WHERE id=? AND (last_signal_id IS NULL OR (last_signal_id<>? AND last_signal_id<>?))",(claim,row["id"],sid,claim))
                claimed=getattr(cur,"rowcount",1)>0
                conn.commit(); conn.close()
            if not claimed: continue
            try:
                _queue_and_execute_ctrader(row,sig,automatic=True)
            except Exception as exec_exc:
                with DB_LOCK:
                    conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET last_signal_id=NULL,last_error=?,updated_at=? WHERE id=? AND last_signal_id=?",("Auto-trade execution failed; retrying: "+str(exec_exc)[:420],_now_iso(),row["id"],claim)); conn.commit(); conn.close()
                raise
        except Exception as exc:
            app.logger.warning("cTrader auto-trade skipped: %s",exc)
            with DB_LOCK:
                conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET last_error=?,updated_at=? WHERE id=?",(str(exc)[:500],_now_iso(),row["id"])); conn.commit(); conn.close()


def _ctrader_autotrade_loop():
    # The browser never owns this loop. It runs continuously in the KETS web
    # service and consumes the same READY signals stored in Signal History.
    while True:
        try:
            _ctrader_autotrade_once()
            time.sleep(1)
        except Exception:
            app.logger.exception("cTrader auto-trade loop error")
            time.sleep(3)

def _ctrader_close_positions(row, auto_only=False):
    accounts=json.loads(row.get("account_ids_json") or "[]")
    selected=str(row.get("selected_account_id") or "")
    account=next((a for a in accounts if str(a.get("id"))==selected), None)
    if not account: raise RuntimeError("No cTrader account selected. Connect cTrader and choose an account first.")
    access_token=_ctrader_fernet().decrypt(row["access_token_enc"].encode()).decode()
    is_live=bool(account.get("is_live"))
    try: positions=json.loads(row.get("positions_json") or "[]")
    except Exception: positions=[]
    # For an automatic signal reversal, refresh the broker positions first so
    # an entry opened only moments ago cannot be missed by a stale dashboard cache.
    if auto_only:
        try:
            fresh=_ctrader_account_snapshot(row)
            positions=fresh.get("positions",[])
        except Exception:
            pass
        positions=[p for p in positions if str(p.get("label") or "").upper()=="KETS_AUTO" or str(p.get("comment") or "").upper().startswith("KETS AUTOMATIC") ]
    async def run():
        import websockets
        async with websockets.connect(_ctrader_host(is_live),open_timeout=15,close_timeout=5,ping_interval=20,ping_timeout=20) as ws:
            async def send(pt,payload):
                await ws.send(json.dumps({"clientMsgId":str(uuid.uuid4()),"payloadType":pt,"payload":payload}))
                for _ in range(12):
                    d=json.loads(await asyncio.wait_for(ws.recv(),timeout=2.5))
                    if d.get("payloadType")==51: continue
                    if d.get("payloadType") in (2132,2142):
                        ep=d.get("payload") or {}
                        raise RuntimeError(ep.get("description") or ep.get("errorCode") or "cTrader request failed")
                    if d.get("payloadType")==2126:
                        ep=d.get("payload") or {}
                        execution_type=int(ep.get("executionType") or 0)
                        if execution_type in (7,8):
                            raise RuntimeError(ep.get("description") or "cTrader rejected the request.")
                    return d
                raise RuntimeError("cTrader response timeout")
            r=await send(2100,{"clientId":_ctrader_client_id(),"clientSecret":_ctrader_client_secret()})
            if r.get("payloadType")!=2101: raise RuntimeError("cTrader application authorization failed")
            r=await send(2102,{"ctidTraderAccountId":int(selected),"accessToken":access_token})
            if r.get("payloadType")!=2103: raise RuntimeError((r.get("payload") or {}).get("description") or "cTrader account authorization failed")
            results=[]
            for pos in positions:
                pid=pos.get("positionId") or pos.get("id")
                vol=pos.get("volume_cents")
                if not pid: continue
                volume=int(round(float(vol or 0)))
                if volume<=0: volume=int(round(float(pos.get("volume",0))*100))
                results.append(await send(2111,{"ctidTraderAccountId":int(selected),"positionId":int(pid),"volume":volume}))
            return results
    return asyncio.run(run())

def _ctrader_close_all_positions(row):
    return _ctrader_close_positions(row, auto_only=False)

@app.route("/api/managed/manual-order", methods=["POST"])
def managed_manual_order():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    row=_ctrader_row_for_user(user["id"])
    if not row or row.get("status") not in ("AUTHORIZED","CONNECTED"): return jsonify({"error":"Connect cTrader and select an account first."}),400
    body=request.get_json(silent=True) or {}
    direction=str(body.get("direction") or "").upper()
    symbol=str(body.get("symbol") or "XAUUSD").replace("/","").upper()
    if direction not in ("BUY","SELL"): return jsonify({"error":"Choose BUY or SELL."}),400
    if symbol not in ("XAUUSD","BTCUSD"): return jsonify({"error":"Unsupported symbol."}),400
    # Manual lot size is intentionally independent from the automatic-entry
    # lot size. The browser sends the value the trader entered in Manual Trade.
    try: volume=float(body.get("volume"))
    except Exception: volume=_num(row.get("lot_size")) or .01
    if not math.isfinite(volume) or volume < 0.01 or volume > 10:
        return jsonify({"error":"Manual lot size must be between 0.01 and 10 lots."}),400
    order={"symbol":symbol,"direction":direction,"volume":volume,"stop_loss":body.get("stop_loss"),"take_profit":body.get("take_profit")}
    try:
        result,selected=_ctrader_execute_order(row,order)
        with DB_LOCK:
            conn=db_conn(); db_execute(conn,"UPDATE ctrader_connections SET selected_account_id=?,last_error=NULL,updated_at=? WHERE id=?",(selected,_now_iso(),row["id"])); conn.commit(); conn.close()
        _ctrader_invalidate_snapshot(row)
        return jsonify({"ok":True,"broker":"cTrader","result":result})
    except Exception as exc:
        return jsonify({"error":str(exc)}),400

@app.route("/api/managed/close-all", methods=["POST"])
def managed_close_all():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    row=_ctrader_row_for_user(user["id"])
    if not row: return jsonify({"error":"Connect cTrader first."}),400
    try:
        result=_ctrader_close_all_positions(row)
        return jsonify({"ok":True,"broker":"cTrader","closed_requests":len(result)})
    except Exception as exc:
        return jsonify({"error":str(exc)}),400

@app.route("/api/managed/queue-current", methods=["POST"])
def managed_queue_current():
    user=_current_user()
    if not user: return jsonify({"error":"Sign in required."}),401
    row=_ctrader_row_for_user(user["id"])
    if not row: return jsonify({"error":"Connect cTrader first."}),400
    sig=_current_signal()
    if not sig: return jsonify({"error":"No current KETS signal is available."}),400
    try:
        result=_queue_and_execute_ctrader(row,sig,automatic=False)
        return jsonify({"ok":True,"broker":"cTrader","result":result})
    except Exception as exc:
        return jsonify({"error":str(exc)}),400

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
    if user.get("developer"):
        return jsonify({"payments": [], "currency": "UGX"})
    with DB_LOCK:
        conn = db_conn()
        rows = db_execute(conn, "SELECT tx_ref,plan,amount,currency,status,network,created_at,updated_at,tracking_id FROM payments WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(user["id"],)).fetchall()
        conn.close()
    return jsonify({"payments":[dict(r) for r in rows], "currency":"UGX"})

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

@app.route("/api/status", methods=["GET"])
def api_status():
    now = get_eat_time()
    active = trading_hours_open(now)
    if active:
        seconds_to_boundary = max(0, int((datetime.datetime.combine(now.date(), datetime.time(18, 0), tzinfo=EAT) - now).total_seconds()))
    else:
        start = datetime.datetime.combine(now.date(), datetime.time(6, 0), tzinfo=EAT)
        if now >= datetime.datetime.combine(now.date(), datetime.time(18, 0), tzinfo=EAT):
            start += datetime.timedelta(days=1)
        seconds_to_boundary = max(0, int((start - now).total_seconds()))
    return jsonify({
        "ok": True,
        "engine_running": bool(engine_started) if "engine_started" in globals() else os.environ.get("KETS_DISABLE_ENGINE", "0") != "1",
        "server_time": now.isoformat(),
        "time_eat": now.isoformat(),
        "last_scan": last_scan,
        "next_scan": next_scan,
        "next_broadcast_seconds": max(0, int((datetime.datetime.fromisoformat(next_scan) - now).total_seconds())) if next_scan else None,
        "signal_window": {
            "active": active,
            "seconds_to_stop": seconds_to_boundary if active else 0,
            "seconds_to_start": seconds_to_boundary if not active else 0,
        },
        "markets": list(get_markets(now).keys()),
    })


def _latest_signal_map(items):
    # Select by actual signal timestamp, not list insertion order. This prevents
    # an older persisted copy from replacing the newest STRONG REVERSAL payload.
    latest = {}
    def stamp(x):
        raw=x.get("timestamp") or x.get("timestamp_utc") or x.get("created_at")
        try:
            dt=datetime.datetime.fromisoformat(str(raw).replace("Z","+00:00"))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=EAT)
            return dt.timestamp()
        except Exception:
            return 0.0
    for item in items:
        asset = str(item.get("asset") or item.get("market") or "").upper()
        if not asset:
            continue
        current=latest.get(asset)
        if current is None or stamp(item) >= stamp(current):
            latest[asset] = dict(item)
    return latest


def _history_items():
    cutoff = get_eat_time() - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
    with API_LOCK:
        memory = [dict(x) for x in SIGNAL_HISTORY]
    persistent = _load_persistent_signals()
    merged = {}
    for item in persistent + memory:
        ts = item.get("timestamp") or item.get("timestamp_utc") or item.get("created_at")
        try:
            dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")) if ts else None
            if dt and dt.tzinfo is None: dt = dt.replace(tzinfo=EAT)
            if dt and dt < cutoff: continue
        except Exception:
            pass
        key = str(item.get("id") or f"{item.get('asset')}-{item.get('direction')}-{item.get('timestamp')}")
        merged[key] = item
    result = list(merged.values())
    result.sort(key=lambda x: str(x.get("timestamp") or x.get("created_at") or ""))
    return result[-500:]


@app.route("/api/signals", methods=["GET", "POST"])
def api_signals():
    if request.method == "POST":
        require_auth = os.environ.get("KETS_REQUIRE_SIGNAL_AUTH", "0") == "1"
        if require_auth:
            supplied = request.headers.get("X-KETS-API-KEY", "").strip()
            expected = os.environ.get("KETS_SIGNAL_RECEIVER_KEY", "").strip() or _source_config()[1]
            if not expected or not secrets.compare_digest(supplied, expected):
                return jsonify({"error": "Signal receiver authentication failed."}), 401
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "JSON signal payload required."}), 400
        asset = str(body.get("asset") or body.get("market") or "").strip().upper()
        direction = str(body.get("direction") or "").strip().upper()
        if not asset or direction not in {"BUY", "SELL"}:
            return jsonify({"error": "Signal must include asset and BUY/SELL direction."}), 400
        now = get_eat_time()
        item = dict(body)
        item["asset"] = asset
        item["market"] = item.get("market") or asset
        item["direction"] = direction
        item["score"] = _num(item.get("score", item.get("strength", 0)))
        item["strength"] = item.get("strength", item["score"])
        item = normalize_signal_payload(item, asset)
        item["timestamp"] = str(item.get("timestamp") or item.get("timestamp_utc") or now.isoformat())
        item["id"] = str(item.get("id") or f"{asset}-{direction}-{item['timestamp']}")
        with API_LOCK:
            existing = {str(x.get("id")) for x in SIGNAL_HISTORY}
            if item["id"] not in existing:
                SIGNAL_HISTORY.append(item)
                cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
                kept = []
                for x in SIGNAL_HISTORY:
                    try:
                        dt = datetime.datetime.fromisoformat(str(x.get("timestamp")).replace("Z", "+00:00"))
                        if dt.tzinfo is None: dt = dt.replace(tzinfo=EAT)
                        if dt >= cutoff: kept.append(x)
                    except Exception:
                        kept.append(x)
                SIGNAL_HISTORY[:] = kept
        _persist_signal(item)
        return jsonify({"ok": True, "accepted": True, "signal": item}), 200

    # The private website bridge must be able to read the bot feed without
    # pretending to be a subscribed browser user. Browser requests still use
    # the normal paid/developer access gate.
    if not _source_request_authorized():
        user, error = _require_active_access()
        if error:
            return error
    history = _history_items()
    return jsonify({
        "ok": True,
        "signals": _latest_signal_map(history),
        "history": history,
        "markets": list(get_markets().keys()),
        "time_eat": get_eat_time().isoformat(),
    })


@app.route("/api/source/signals", methods=["GET"])
def api_source_signals():
    """Private machine-to-machine feed for the trading bot -> website bridge."""
    if not _source_request_authorized():
        return jsonify({"error": "Signal source authentication failed."}), 401
    history = _history_items()
    return jsonify({
        "ok": True,
        "signals": _latest_signal_map(history),
        "history": history,
        "markets": list(get_markets().keys()),
        "time_eat": get_eat_time().isoformat(),
        "source": "KETS private strategy feed",
    })

@app.route("/api/public/welcome", methods=["GET"])
def api_public_welcome():
    """Public read-only feed for the sign-in/welcome page.
    Uses the same signal history and 06:00-18:00 EAT window as the dashboard.
    No authentication is required because this endpoint only powers the
    public preview on the welcome screen.
    """
    now = get_eat_time()
    active = trading_hours_open(now)
    if active:
        seconds_to_boundary = max(0, int((
            datetime.datetime.combine(now.date(), datetime.time(18, 0), tzinfo=EAT) - now
        ).total_seconds()))
    else:
        start = datetime.datetime.combine(now.date(), datetime.time(6, 0), tzinfo=EAT)
        if now >= datetime.datetime.combine(now.date(), datetime.time(18, 0), tzinfo=EAT):
            start += datetime.timedelta(days=1)
        seconds_to_boundary = max(0, int((start - now).total_seconds()))
    history = _history_items()
    return jsonify({
        "ok": True,
        "server_time": now.isoformat(),
        "signal_window": {
            "active": active,
            "seconds_to_stop": seconds_to_boundary if active else 0,
            "seconds_to_start": seconds_to_boundary if not active else 0,
        },
        "markets": list(get_markets(now).keys()),
        "history": history[-30:],
    })


@app.route("/api/history", methods=["GET"])
def api_history():
    user, error = _require_active_access()
    if error:
        return error
    return jsonify({"ok": True, "history": _history_items(), "days": SIGNAL_HISTORY_DAYS})


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
    "30_min": {"name": "30 Minutes", "ugx": 10000, "seconds": 30*60},
    "1_hour": {"name": "1 Hour", "ugx": 30000, "usd": 10, "seconds": 60*60},
    "1_day": {"name": "1 Day", "ugx": 50000, "usd": 50, "seconds": 24*60*60},
    "1_week": {"name": "1 Week", "ugx": 400000, "usd": 200, "seconds": 7*24*60*60},
    "1_month": {"name": "1 Month", "ugx": 2000000, "usd": 500, "seconds": 30*24*60*60},
}
def _subscription_expiry(plan_id, start_iso):
    try:
        return datetime.datetime.fromisoformat(start_iso) + datetime.timedelta(seconds=PAYMENT_PLANS[plan_id]["seconds"])
    except Exception:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

def _user_access(user_id):
    if user_id == "__kets_developer__":
        return {"plan": "developer", "expires": None, "tx_ref": None, "developer": True}
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
    secret = os.environ.get("KETS_SESSION_SECRET") or os.environ.get("PESAPAL_CONSUMER_SECRET")
    # A deterministic fallback is intentionally not used. Render should provide
    # KETS_SESSION_SECRET; otherwise tokens could be forged or invalidated after
    # a restart. The fallback only keeps local development bootable.
    return secret or "LOCAL-DEV-ONLY-CHANGE-ME"


def _serializer():
    return URLSafeTimedSerializer(_payment_secret(), salt="kets-access-v1")


def _access_token(plan_id, tx_ref, user_id, expires=None):
    plan = PAYMENT_PLANS[plan_id]
    if expires is None:
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
    user = _current_user()
    if not user:
        return False
    return _user_access(user["id"]) is not None

def _require_active_access(user=None):
    """Require authentication and a currently active entitlement.

    Developer access is permanent/free. Normal-user access is checked against
    the server-side completed-payment records on every protected data request,
    so an old session token cannot keep reading live signals after expiry.
    """
    user = user or _current_user()
    if not user:
        return None, (jsonify({"error": "Sign in required."}), 401)
    if user.get("developer"):
        return user, None
    access = _user_access(user["id"])
    if not access:
        return user, (jsonify({
            "error": "An active payment plan is required to access live KETS signals.",
            "payment_required": True,
            "plans_url": "/api/plans",
        }), 402)
    return user, None


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
    """Verify a Pesapal payment and grant access exactly once.

    A COMPLETED payment is terminal: later callback/IPN/verify requests must
    never refresh updated_at or extend the subscription from the same order.
    """
    plan_id = _plan_from_ref(tx_ref)
    if not plan_id or not tracking_id:
        return None

    # Load the locally-created order first so an unknown reference can never
    # be turned into an entitlement.
    with DB_LOCK:
        conn = db_conn()
        order = db_execute(conn, "SELECT * FROM payments WHERE tx_ref=?", (tx_ref,)).fetchone()
        conn.close()
    if not order:
        return {"paid": False, "status": "UNKNOWN",
                "message": "KETS could not match this payment to an account."}

    plan = PAYMENT_PLANS[plan_id]

    # A completed order is immutable. We can safely return its existing
    # entitlement without changing its timestamp, preventing replay/extension.
    if str(order["status"] or "").upper() == "COMPLETED":
        expires = _subscription_expiry(plan_id, order["updated_at"])
        token = _access_token(plan_id, tx_ref, order["user_id"], expires=expires)
        return {
            "paid": expires > get_eat_time(),
            "token": token,
            "plan": plan_id,
            "expires": expires.isoformat(),
            "status": "COMPLETED",
            "message": "Payment already confirmed; existing access was preserved.",
        }

    status = _pesapal_status(tracking_id)
    status_text = str(status.get("payment_status_description", "")).upper()
    status_code = status.get("status_code")
    try:
        amount = float(status.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0.0

    expected_currency = str(order["currency"] or "UGX").upper()
    expected_amount = float(order["amount"] or 0)
    valid = (
        (status_code == 1 or status_text == "COMPLETED")
        and str(status.get("merchant_reference", "")) == tx_ref
        and str(status.get("currency", "")).upper() == expected_currency
        and abs(amount - expected_amount) < 0.01
    )

    # Only the transition to COMPLETED sets the entitlement timestamp.
    # Non-completed checks may update status, but never grant access.
    with DB_LOCK:
        conn = db_conn()
        current = db_execute(conn, "SELECT status,updated_at FROM payments WHERE tx_ref=?", (tx_ref,)).fetchone()
        if current and str(current["status"] or "").upper() == "COMPLETED":
            # Another callback/IPN may have completed it concurrently.
            conn.close()
            expires = _subscription_expiry(plan_id, current["updated_at"])
            token = _access_token(plan_id, tx_ref, order["user_id"], expires=expires)
            return {
                "paid": expires > get_eat_time(), "token": token, "plan": plan_id,
                "expires": expires.isoformat(), "status": "COMPLETED",
                "message": "Payment already confirmed; existing access was preserved.",
            }

        if valid:
            completed_at = _now_iso()
            db_execute(conn,
                "UPDATE payments SET tracking_id=?,status='COMPLETED',amount=?,updated_at=? WHERE tx_ref=?",
                (tracking_id, amount, completed_at, tx_ref))
            expires = _subscription_expiry(plan_id, completed_at)
        else:
            db_execute(conn,
                "UPDATE payments SET tracking_id=?,status=?,amount=? WHERE tx_ref=?",
                (tracking_id, status_text or str(status_code or "PENDING"), amount, tx_ref))
            expires = None
        conn.commit()
        conn.close()

    if not valid:
        return {"paid": False, "status": status_text or str(status_code or "UNKNOWN"),
                "message": "Payment is not yet confirmed or does not match this KETS plan."}

    token = _access_token(plan_id, tx_ref, order["user_id"], expires=expires)
    return {"paid": True, "token": token, "plan": plan_id,
            "expires": expires.isoformat(),
            "message": "Payment confirmed. KETS live signals are unlocked."}


@app.route("/api/access")
def api_access():
    user=_current_user()
    access=_user_access(user["id"]) if user else None
    return jsonify({
        "authenticated":bool(user),
        "paid":bool(access),
        "mode":"paid" if access else "locked",
        "plan":access.get("plan") if access else None,
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
    if user.get("developer"):
        return jsonify({"error":"Developer access is free; no payment is required."}),400
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



@app.route("/api/payments/create-public", methods=["POST"])
def api_payment_create_public():
    """Create a payment for an already-registered account before sign-in.

    This endpoint never grants access. Pesapal confirmation is still required
    before the normal login gate will accept the account.
    """
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    plan_id = str(body.get("plan", "")).lower()
    phone = str(body.get("phone", "")).strip()
    network = str(body.get("network", "")).upper().strip()

    if not _valid_email(email):
        return jsonify({"error": "Enter the email used to create your KETS account."}), 400
    if plan_id not in PAYMENT_PLANS:
        return jsonify({"error": "Invalid plan."}), 400

    with DB_LOCK:
        conn = db_conn()
        user = db_execute(conn, "SELECT * FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        conn.close()
    if not user:
        return jsonify({"error": "Create your KETS account first, then choose a payment plan."}), 404

    is_uganda = str(user["country_code"] or "").upper() == "UG"
    plan = PAYMENT_PLANS[plan_id]
    if not is_uganda and not plan.get("usd"):
        return jsonify({"error": "This plan is currently available only to users in Uganda."}), 400

    if is_uganda:
        if network not in {"MTN", "AIRTEL"}:
            return jsonify({"error": "Choose MTN or AIRTEL."}), 400
        if len(re.sub(r"\D", "", phone)) < 9:
            return jsonify({"error": "Enter a valid Uganda mobile-money number."}), 400
    else:
        network = "INTERNATIONAL"

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
            "country_code": str(user["country_code"] or "UG").upper(),
            "first_name": "KETS",
            "last_name": "Customer",
            "line_1": "KETS Online",
            "city": "Kampala",
        },
    }
    if not payload["notification_id"]:
        return jsonify({"error": "PESAPAL_IPN_ID is not configured."}), 503

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
    now = _now_iso()
    with DB_LOCK:
        conn = db_conn()
        values = (str(uuid.uuid4()), user["id"], tx_ref, tracking_id, plan_id, amount_to_pay,
                  currency, "PENDING", network, email, phone, now, now)
        if using_postgres():
            db_execute(conn, """INSERT INTO payments(id,user_id,tx_ref,tracking_id,plan,amount,currency,status,network,email,phone,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT (tx_ref) DO UPDATE SET tracking_id=EXCLUDED.tracking_id, updated_at=EXCLUDED.updated_at""", values)
        else:
            db_execute(conn, "INSERT OR REPLACE INTO payments(id,user_id,tx_ref,tracking_id,plan,amount,currency,status,network,email,phone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        conn.commit()
        conn.close()

    return jsonify({
        "ok": True,
        "tx_ref": tx_ref,
        "order_tracking_id": tracking_id,
        "redirect_url": data.get("redirect_url"),
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
        return f"""<!doctype html><html><head><meta charset='utf-8'><title>KETS Payment</title></head><body><p>Payment processed. Returning to KETS…</p><script>window.location.replace('/?payment=success#login={user_token}');</script></body></html>"""

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
    """Persist a signal received by the website into SQLite on Render storage.

    Flow: trading bot -> /api/signals -> KETS website -> Render persistent disk.
    The browser never writes signals directly to the database. The complete
    normalized signal is retained in payload JSON and signal IDs are idempotent.
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


def normalize_signal_payload(item, asset=None):
    """Normalize every signal alias used by Telegram, website and persistence."""
    item=dict(item or {})
    if asset: item["asset"]=str(item.get("asset") or item.get("market") or asset).upper()
    item["market"]=item.get("market") or item.get("asset")
    item["direction"]=str(item.get("direction") or item.get("signal") or item.get("side") or "").upper()
    raw_status=item.get("status",item.get("execution_status",item.get("entry_exit_status")))
    if raw_status not in (None, ""):
        item["status"]=str(raw_status).upper()
    else:
        entry=_num(item.get("market_price") or item.get("price") or item.get("entry"))
        tp=_num(item.get("take_profit") or item.get("tp") or item.get("target"))
        sl=_num(item.get("stop_loss") or item.get("sl") or item.get("stop"))
        item["status"]="READY" if item["direction"] in {"BUY","SELL"} and entry and tp and sl else "WAITING"
    # Keep the bot's original display status (normally ACTIVE), while giving
    # the execution worker an explicit state. A complete BUY/SELL signal from
    # the existing bot is ready for Auto-Trade without requiring a bot update.
    if item["direction"] in {"BUY","SELL"}:
        entry=_num(item.get("market_price") or item.get("price") or item.get("entry"))
        tp=_num(item.get("take_profit") or item.get("tp") or item.get("target"))
        sl=_num(item.get("stop_loss") or item.get("sl") or item.get("stop"))
        if item.get("execution_status") in (None, ""):
            item["execution_status"]="READY" if entry and tp and sl else "WAITING"
    item["score"]=_num(item.get("score",item.get("strength",item.get("signal_strength",item.get("confidence",0)))))
    item["strength"]=item.get("strength",item["score"])
    aliases={
      "entry":("entry","entry_price","entryPrice","market_price","marketPrice","price","current_price","currentPrice"),
      "take_profit":("take_profit","takeProfit","target","target_price","targetPrice","tp"),
      "stop_loss":("stop_loss","stopLoss","sl","stop_price","stopPrice"),
      "expected_move":("expected_move","expectedMove","price_move","priceMove","move"),
      "expected_move_pct":("expected_move_pct","expectedMovePct","price_move_pct","priceMovePct","move_pct","movePct"),
      "estimated_duration":("estimated_duration","estimatedDuration","duration_text","durationText","duration"),
      "interpretation":("interpretation","signal_interpretation","signalInterpretation","description"),
      "signal_type":("signal_type","signalType","type"),
      "classification":("classification","setup","setup_classification","setupClassification"),
    }
    for dest,keys in aliases.items():
        if item.get(dest) in (None,""):
            for key in keys:
                if item.get(key) not in (None,""):
                    item[dest]=item[key]; break
    item["price_move"]=item.get("price_move",item.get("expected_move"))
    item["price_move_pct"]=item.get("price_move_pct",item.get("expected_move_pct"))
    sr=item.get("strong_reversal",item.get("reversal_signal",False))
    item["strong_reversal"]=(sr is True or str(sr).lower()=="true" or "STRONG REVERSAL" in str(item.get("signal_type") or item.get("classification") or "").upper())
    item["reversal_signal"]=item["strong_reversal"]
    if item["strong_reversal"]:
        item["signal_type"]="STRONG REVERSAL ENTRY"
        item["classification"]=item.get("classification") or "NEW STRONG REVERSAL — price action, momentum and structure are turning together."
    return item

def store_app_signal(asset, signal):
    """Store the COMPLETE strategy payload for the KETS dashboard.

    Telegram receives the rich signal payload, so the website must not reduce
    it to only entry/TP/SL.  Preserve Strong Reversal, intelligence, evidence,
    entry-quality and timing fields exactly as produced by analyze_market.
    """
    now = get_eat_time()
    item = normalize_signal_payload(signal, asset)
    item["entry"] = item.get("entry", item.get("price", item.get("current_price")))
    item["price"] = item.get("price", item.get("entry"))
    item["current_price"] = item.get("current_price", item.get("entry"))
    item["take_profit"] = item.get("take_profit")
    item["stop_loss"] = item.get("stop_loss")
    item["price_move"] = item.get("price_move", item.get("expected_move"))
    item["price_move_pct"] = item.get("price_move_pct", item.get("expected_move_pct"))
    item["expected_move"] = item.get("expected_move", item.get("price_move"))
    item["expected_move_pct"] = item.get("expected_move_pct", item.get("price_move_pct"))
    item["estimated_duration"] = item.get("estimated_duration", item.get("duration_text"))
    item["timestamp"] = str(item.get("timestamp") or item.get("timestamp_utc") or now.isoformat())
    item["timestamp_utc"] = item.get("timestamp_utc", item["timestamp"])
    item["id"] = str(item.get("id") or f"{item['asset']}-{item['direction']}-{item['timestamp']}")
    item["strong_reversal"] = bool(item.get("strong_reversal", item.get("reversal_signal", False)))
    item["reversal_signal"] = item["strong_reversal"]
    if item["strong_reversal"]:
        item["signal_type"] = "STRONG REVERSAL ENTRY"
        item["classification"] = item.get("classification") or "NEW STRONG REVERSAL — price action, momentum and structure are turning together."
    with API_LOCK:
        existing_ids = {str(x.get("id")) for x in SIGNAL_HISTORY}
        if item["id"] not in existing_ids:
            SIGNAL_HISTORY.append(item)
        else:
            for idx, existing in enumerate(SIGNAL_HISTORY):
                if str(existing.get("id")) == item["id"]:
                    SIGNAL_HISTORY[idx] = item
                    break
        cutoff = now - datetime.timedelta(days=SIGNAL_HISTORY_DAYS)
        SIGNAL_HISTORY[:] = [x for x in SIGNAL_HISTORY if _signal_dt(x) >= cutoff]
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
    if len(candles)<60:return False,"Insufficient candles"
    for c in candles[-40:]:
        vals=[c["open"],c["high"],c["low"],c["close"]]
        if not all(math.isfinite(v) for v in vals):return False,"Invalid price data"
        if c["high"]<c["low"]:return False,"Invalid candle range"
    return True,"GOOD"


def check_overextension(price, ema9, atr):
    if atr<=0:return {"extended":False,"distance":0,"ratio":0}
    d=abs(price-ema9); r=d/atr
    return {"extended":r>=1.5,"distance":d,"ratio":r}




def calculate_entry_quality(
    candles, signal_type, current_price, ema9, atr, adx,
    plus_di, minus_di, direction_5m, direction_15m, momentum,
    candle_info, vwap, extension
):
    """Strict 0-100 entry-location gate.

    This is intentionally separate from the core strategy score.  It answers
    one question: is *this exact 1-minute candle* a good place to enter now?
    The score has a fixed 100-point denominator so missing volume/VWAP data can
    never inflate the result.
    """
    closes = [c["close"] for c in candles]
    reasons = []
    points = 0.0

    def aligned(direction):
        return (
            (signal_type == "BUY" and direction == "BULLISH") or
            (signal_type == "SELL" and direction == "BEARISH")
        )

    # 20 pts — higher-timeframe agreement. Both are required by the hard gate.
    htf5 = aligned(direction_5m)
    htf15 = aligned(direction_15m)
    points += 10 if htf5 else 0
    points += 10 if htf15 else 0
    reasons.append("5M trend aligned" if htf5 else "5M trend conflict")
    reasons.append("15M trend aligned" if htf15 else "15M trend conflict")

    # 15 pts — actual entry structure, not merely a bullish/bearish candle.
    ema20 = calculate_ema(closes, 20)
    ema50 = calculate_ema(closes, 50)
    ema_structure = (
        (signal_type == "BUY" and current_price > ema20 > ema50) or
        (signal_type == "SELL" and current_price < ema20 < ema50)
    )
    if ema_structure:
        points += 15
        reasons.append("EMA20/EMA50 entry structure aligned")
    else:
        reasons.append("EMA20/EMA50 entry structure not aligned")

    # 15 pts — trend strength + DI direction. ADX below 20 is not a quality
    # early entry even when the raw strategy score happens to be high.
    di_ok = (
        (signal_type == "BUY" and plus_di > minus_di) or
        (signal_type == "SELL" and minus_di > plus_di)
    )
    previous_adx = None
    if len(candles) >= 41:
        previous_adx = calculate_adx(candles[:-1], 14).get("adx")
    adx_rising = previous_adx is not None and adx > previous_adx
    if adx >= 25 and di_ok and (adx_rising or previous_adx is None):
        points += 15
        reasons.append("ADX strong, DI aligned and trend strengthening")
    elif adx >= 25 and di_ok:
        points += 12
        reasons.append("ADX strong and DI aligned")
    elif adx >= 20 and di_ok:
        points += 8
        reasons.append("ADX developing and DI aligned")
    else:
        reasons.append("ADX/DI trend confirmation weak")

    # 10 pts — candle close quality. Prefer decisive closes without requiring
    # every signal to be a breakout, preserving the early-entry objective.
    candle_ok = False
    breakout_close = False
    if candles:
        cur = candles[-1]
        full_range = cur["high"] - cur["low"]
        if full_range > 0:
            close_position = (cur["close"] - cur["low"]) / full_range
            strong_buy = signal_type == "BUY" and close_position >= 0.65 and cur["close"] > cur["open"]
            strong_sell = signal_type == "SELL" and close_position <= 0.35 and cur["close"] < cur["open"]
            breakout_close = (
                signal_type == "BUY" and cur["close"] > candles[-2]["high"]
            ) or (
                signal_type == "SELL" and cur["close"] < candles[-2]["low"]
            )
            candle_ok = breakout_close or (strong_buy or strong_sell) and candle_info.get("strength", 0) >= 55
    if breakout_close:
        points += 10
        reasons.append("Breakout candle closed with confirmation")
    elif candle_ok:
        points += 7
        reasons.append("Directional candle close confirmed")
    else:
        reasons.append("Candle close lacks decisive entry confirmation")

    # 10 pts — momentum must agree; accelerating gets full credit.
    momentum_aligned = aligned(momentum.get("direction"))
    if momentum_aligned:
        state = momentum.get("state")
        if state == "ACCELERATING":
            points += 10
            reasons.append("Momentum aligned and accelerating")
        elif state == "STABLE":
            points += 8
            reasons.append("Momentum aligned and stable")
        else:
            points += 5
            reasons.append("Momentum aligned but weakening")
    else:
        reasons.append("Momentum direction conflict")

    # 10 pts — healthy RSI entry zone. Avoid buying exhaustion / selling
    # exhaustion while still allowing genuine early entries.
    rsi_now = calculate_rsi(closes)
    rsi_entry_ok = (
        (signal_type == "BUY" and 42 <= rsi_now <= 68) or
        (signal_type == "SELL" and 32 <= rsi_now <= 58)
    )
    rsi_ideal = (
        (signal_type == "BUY" and 48 <= rsi_now <= 62) or
        (signal_type == "SELL" and 38 <= rsi_now <= 52)
    )
    if rsi_ideal:
        points += 10
        reasons.append(f"RSI in ideal early-entry zone ({rsi_now:.1f})")
    elif rsi_entry_ok:
        points += 7
        reasons.append(f"RSI in acceptable entry zone ({rsi_now:.1f})")
    else:
        reasons.append(f"RSI weak/exhausted for entry ({rsi_now:.1f})")

    # 10 pts — price-location / chase protection. This is a major difference
    # between trend quality and entry quality.
    extension_ratio = extension.get("ratio", 0.0) if isinstance(extension, dict) else 0.0
    if extension.get("extended", False):
        reasons.append(f"Price too far from EMA9 ({extension_ratio:.2f} ATR)")
    elif extension_ratio <= 0.75:
        points += 10
        reasons.append(f"Entry location clean ({extension_ratio:.2f} ATR from EMA9)")
    elif extension_ratio <= 1.10:
        points += 7
        reasons.append(f"Entry location acceptable ({extension_ratio:.2f} ATR from EMA9)")
    else:
        points += 3
        reasons.append(f"Entry location getting stretched ({extension_ratio:.2f} ATR)")

    # 5 pts — 3-candle directional structure.
    structure_ok = False
    if len(candles) >= 4:
        a, b, c = candles[-3], candles[-2], candles[-1]
        structure_ok = (
            (signal_type == "BUY" and b["high"] >= a["high"] and b["low"] >= a["low"] and c["high"] >= b["high"] and c["low"] >= b["low"]) or
            (signal_type == "SELL" and b["high"] <= a["high"] and b["low"] <= a["low"] and c["high"] <= b["high"] and c["low"] <= b["low"])
        )
    if structure_ok:
        points += 5
        reasons.append("3-candle market structure confirms direction")
    else:
        reasons.append("3-candle structure not fully confirmed")

    # 3 pts — volume is useful when supplied, but never required for markets
    # where the feed does not provide trustworthy volume.
    volume_score = 0
    valid_volumes = [
        c.get("volume") for c in candles
        if isinstance(c.get("volume"), (int, float)) and math.isfinite(c.get("volume")) and c.get("volume") > 0
    ]
    if len(valid_volumes) >= 21 and isinstance(candles[-1].get("volume"), (int, float)):
        prev = [c["volume"] for c in candles[-21:-1] if isinstance(c.get("volume"), (int, float)) and c["volume"] > 0]
        if prev:
            ratio = candles[-1]["volume"] / (sum(prev) / len(prev))
            if ratio >= 1.5:
                volume_score = 3
                reasons.append(f"Volume expansion confirmed ({ratio:.2f}x)")
            elif ratio >= 1.0:
                volume_score = 1
                reasons.append(f"Volume present ({ratio:.2f}x average)")
            else:
                reasons.append(f"Volume below average ({ratio:.2f}x)")
    else:
        reasons.append("Volume unavailable — not used as a penalty")
    points += volume_score

    # 2 pts — VWAP location when volume/VWAP is trustworthy.
    if vwap is not None:
        vwap_ok = (
            (signal_type == "BUY" and current_price > vwap) or
            (signal_type == "SELL" and current_price < vwap)
        )
        if vwap_ok:
            points += 2
            reasons.append("VWAP aligned")
        else:
            reasons.append("VWAP conflict")
    else:
        reasons.append("VWAP unavailable — not used as a penalty")

    # Reversal veto: a strong opposite candle AND opposite momentum is enough
    # to invalidate the setup regardless of score.
    opposite_candle = (
        (signal_type == "BUY" and candle_info.get("direction") == "BEARISH") or
        (signal_type == "SELL" and candle_info.get("direction") == "BULLISH")
    )
    opposite_momentum = (
        (signal_type == "BUY" and momentum.get("direction") == "BEARISH") or
        (signal_type == "SELL" and momentum.get("direction") == "BULLISH")
    )
    clear_reversal = opposite_candle and opposite_momentum and candle_info.get("strength", 0) >= 55
    if clear_reversal:
        reasons.append("CLEAR REVERSAL WARNING — entry veto")

    # Fixed denominator: 100 possible points regardless of feed features.
    quality_score = round(max(0.0, min(100.0, points)))

    # Mandatory gates prevent a high aggregate score from masking a bad entry.
    hard_fail = []
    if not htf5: hard_fail.append("5M conflict")
    if not htf15: hard_fail.append("15M conflict")
    if not ema_structure: hard_fail.append("EMA structure")
    if adx < 20 or not di_ok: hard_fail.append("ADX/DI")
    if not rsi_entry_ok: hard_fail.append("RSI zone")
    if extension.get("extended", False): hard_fail.append("overextended")
    if clear_reversal: hard_fail.append("reversal")
    if not candle_ok: hard_fail.append("candle confirmation")

    if hard_fail:
        status = "REJECT — " + ", ".join(hard_fail[:3])
    elif quality_score >= 85:
        status = "A+ HIGH QUALITY ENTRY"
    elif quality_score >= 78:
        status = "HIGH QUALITY ENTRY"
    elif quality_score >= 72:
        status = "ACCEPTABLE ENTRY"
    else:
        status = "LOW QUALITY ENTRY"

    return {
        "score": quality_score,
        "status": status,
        "clear_reversal": clear_reversal,
        "hard_fail": hard_fail,
        "ema20": ema20,
        "ema50": ema50,
        "adx_rising": adx_rising,
        "previous_adx": previous_adx,
        "reasons": reasons,
    }

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
        # Never score the still-forming 1-minute candle. Twelve Data can
        # return the current minute, which makes crossover/candle quality
        # flicker and creates false early entries.
        completed=[]
        now_utc=datetime.datetime.now(datetime.timezone.utc).replace(second=0,microsecond=0)
        for c in out:
            raw=str(c.get("datetime", "")).strip()
            try:
                stamp=datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                try:
                    stamp=datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
                except Exception:
                    continue
            if stamp.tzinfo is None:
                stamp=stamp.replace(tzinfo=datetime.timezone.utc)
            if stamp.astimezone(datetime.timezone.utc).replace(second=0,microsecond=0) < now_utc:
                completed.append(c)
        return completed
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
    if len(candles)<60:return None
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
    entry_quality=calculate_entry_quality(candles,direction,price,ema9,atr,ad["adx"],ad["plus_di"],ad["minus_di"],d5,d15,mom,ci,vwap,ext)
    entry_quality_score=entry_quality["score"]
    entry_quality_status=entry_quality["status"]
    entry_quality_reasons=entry_quality["reasons"]

    # Dashboard-only Strong Reversal evidence. Existing strategy selection is
    # intentionally untouched; these values only describe an already-created signal.
    breakout_close = (
        (direction=="BUY" and cur["close"] > prev["high"]) or
        (direction=="SELL" and cur["close"] < prev["low"])
    )
    reversal_checks = [
        bool((bullish and direction=="BUY") or (bearish and direction=="SELL")),
        bool(breakout_close),
        bool((mom["direction"]=="BULLISH" and direction=="BUY") or (mom["direction"]=="BEARISH" and direction=="SELL")),
        bool((ebc and direction=="BUY") or (esc and direction=="SELL") or
             (eb and direction=="BUY") or (es and direction=="SELL")),
        bool((bull_cross or rb) if direction=="BUY" else (bear_cross or rs)),
        bool((ad["plus_di"] > ad["minus_di"]) if direction=="BUY" else (ad["minus_di"] > ad["plus_di"]))
    ]
    reversal_evidence_count=sum(1 for x in reversal_checks if x)
    reversal_evidence_total=len(reversal_checks)
    strong_reversal = reversal_evidence_count >= 5 and ci["strength"] >= 55 and not extended
    reversal_reasons=[]
    if reversal_checks[0]: reversal_reasons.append("Strong bullish reversal candle" if direction=="BUY" else "Strong bearish reversal candle")
    if reversal_checks[1]: reversal_reasons.append("Break above previous candle high" if direction=="BUY" else "Break below previous candle low")
    if reversal_checks[2]: reversal_reasons.append("Bullish momentum turned/accelerated" if direction=="BUY" else "Bearish momentum turned/accelerated")
    if reversal_checks[3]: reversal_reasons.append("EMA direction turning bullish" if direction=="BUY" else "EMA direction turning bearish")
    if reversal_checks[4]: reversal_reasons.append("MACD momentum turned bullish" if direction=="BUY" else "MACD momentum turned bearish")
    if reversal_checks[5]: reversal_reasons.append("DI+ moved above DI-" if direction=="BUY" else "DI- moved above DI+")

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
    # Entry Quality is informational, not a 90+ gate.
    # Keep the original KETS core/advanced strategy selection intact while
    # allowing the dashboard to show every calculated entry-quality score,
    # including 40/100, 50/100 and other lower readings.

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
    bot=(f"🤖 *KETS — EARLY ENTRY SIGNAL — {asset}*\n━━━━━━━━━━━━━━━━━━\n📈 *Direction:* {'🟢 BUY / LONG' if direction=='BUY' else '🔴 SELL / SHORT'}\n💯 *Signal Strength:* {score}%\n🧠 *Interpretation:* {interp}\n🏷️ *Setup:* {setup}\n🛡️ *Entry Quality:* {entry_quality_score}/100 — {entry_quality_status}\n━━━━━━━━━━━━━━━━━━\n📍 *Market Price:* ${entry:,.2f}\n🎯 *Take Profit:* ${tp:,.2f}\n🛑 *Stop Loss:* ${sl:,.2f}\n📊 *Expected Price Move:* ${move:,.2f} ({move_pct:.2f}%)\n⏱️ *Estimated Duration:* {duration}\n━━━━━━━━━━━━━━━━━━\n📊 *1-MIN CHECK*\n├ EMA9: ${ema9:,.2f}\n├ EMA26: ${ema26:,.2f}\n├ RSI(14): {rsi:.2f}\n├ MACD: {cm:.5f}\n├ Signal: {cs:.5f}\n└ MACD Status: {macd_status}\n━━━━━━━━━━━━━━━━━━\n🧠 *INTELLIGENCE*\n├ Regime: {regime}\n├ ADX: {ad['adx']:.2f}\n├ DI+: {ad['plus_di']:.2f}\n├ DI-: {ad['minus_di']:.2f}\n├ ATR: ${atr:,.2f}\n├ Momentum: {mom['direction']} / {mom['state']}\n├ Candle: {ci['quality']}\n├ 5M: {d5}\n├ 15M: {d15}\n└ VWAP: {'$'+format(vwap,',.2f') if vwap is not None else 'Unavailable'}\n━━━━━━━━━━━━━━━━━━\n🎯 *LEVELS*\n├ Support: ${levels['support']:,.2f}\n└ Resistance: ${levels['resistance']:,.2f}\n━━━━━━━━━━━━━━━━━━\n🔎 *CORE:*\n" + "\n".join("• "+x for x in reasons) + "\n━━━━━━━━━━━━━━━━━━\n🧠 *ADVANCED:*\n" + "\n".join("• "+x for x in adv) + f"\n━━━━━━━━━━━━━━━━━━\n⏰ {ts}\n⚠️ Strategy-alignment score, not win probability.")
    channel=(f"🤖 *KETS — EARLY ENTRY SIGNAL — {asset}*\n━━━━━━━━━━━━━━━━━━\n📈 *Direction:* {'🟢 BUY / LONG' if direction=='BUY' else '🔴 SELL / SHORT'}\n💯 *Signal Strength:* {score}%\n🧠 *Interpretation:* {interp}\n━━━━━━━━━━━━━━━━━━\n📍 *Market Price:* ${entry:,.2f}\n🎯 *Take Profit:* ${tp:,.2f}\n🛑 *Stop Loss:* ${sl:,.2f}\n📊 *Expected Price Move:* ${move:,.2f} ({move_pct:.2f}%)\n⏱️ *Estimated Duration:* {duration}\n━━━━━━━━━━━━━━━━━━\n⏰ {ts}\n⚠️ Strategy-alignment score, not win probability.")
    return {"asset":asset,"market":asset,"bot":bot,"channel":channel,"direction":direction,"score":score,"strength":score,"entry":entry,"price":entry,"current_price":entry,"take_profit":tp,"stop_loss":sl,"expected_move":move,"expected_move_pct":move_pct,"price_move":move,"price_move_pct":move_pct,"estimated_duration":duration,"interpretation":interp,"setup":setup,"signal_type":"STRONG REVERSAL ENTRY" if strong_reversal else "EARLY ENTRY","strong_reversal":strong_reversal,"reversal_signal":strong_reversal,"classification":"NEW STRONG REVERSAL — price action, momentum and structure are turning together." if strong_reversal else setup,"reversal_evidence_count":reversal_evidence_count,"reversal_evidence_total":reversal_evidence_total,"reversal_reasons":reversal_reasons,"entry_quality_score":entry_quality_score,"entry_quality_status":entry_quality_status,"entry_quality_reversal":entry_quality["clear_reversal"],"entry_quality_reasons":entry_quality_reasons,"entry_quality":{"score":entry_quality_score,"status":entry_quality_status,"clear_reversal":entry_quality["clear_reversal"],"ema20":entry_quality["ema20"],"ema50":entry_quality["ema50"],"adx_rising":entry_quality["adx_rising"],"previous_adx":entry_quality["previous_adx"],"reasons":entry_quality_reasons},"entry_quality_details":{"score":entry_quality_score,"status":entry_quality_status,"ema":{"ema9":ema9,"ema20":entry_quality["ema20"],"ema50":entry_quality["ema50"],"ema26":ema26,"price":price},"trend":{"adx":ad["adx"],"previous_adx":entry_quality["previous_adx"],"plus_di":ad["plus_di"],"minus_di":ad["minus_di"],"adx_rising":entry_quality["adx_rising"],"di_aligned":((ad["plus_di"]>ad["minus_di"]) if direction=="BUY" else (ad["minus_di"]>ad["plus_di"]))},"volume":{"current":cur.get("volume"),"average_20":None,"ratio":None,"available":cur.get("volume") is not None},"candle":{"open":cur["open"],"high":cur["high"],"low":cur["low"],"close":cur["close"],"range":cur["high"]-cur["low"],"close_position":((cur["close"]-cur["low"])/(cur["high"]-cur["low"]) if cur["high"]>cur["low"] else None),"direction":ci["direction"],"strength":ci["strength"],"quality":ci["quality"],"breakout":breakout_close},"momentum":{"direction":mom["direction"],"state":mom["state"],"aligned":((mom["direction"]=="BULLISH") if direction=="BUY" else (mom["direction"]=="BEARISH"))},"vwap":{"value":vwap,"available":vwap is not None,"aligned":((price>vwap) if direction=="BUY" else (price<vwap)) if vwap is not None else None},"extension":ext,"higher_timeframes":{"5m":d5,"15m":d15},"breakout_retest":{"held":None},"reversal":{"clear_reversal":entry_quality["clear_reversal"]}},"strong_reversal":strong_reversal,"reversal_signal":strong_reversal,"signal_type":"STRONG REVERSAL ENTRY" if strong_reversal else "EARLY ENTRY","classification":"NEW STRONG REVERSAL — price action, momentum and structure are turning together." if strong_reversal else setup,"reversal_evidence_count":reversal_evidence_count,"reversal_evidence_total":reversal_evidence_total,"reversal_reasons":reversal_reasons,"ema9":ema9,"ema26":ema26,"ema20":entry_quality["ema20"],"ema50":entry_quality["ema50"],"rsi":rsi,"macd":cm,"macd_signal":cs,"macd_status":macd_status,"market_regime":regime,"adx":ad["adx"],"di_plus":ad["plus_di"],"di_minus":ad["minus_di"],"atr":atr,"momentum_direction":mom["direction"],"momentum_state":mom["state"],"candle_quality":ci["quality"],"timeframe_5m":d5,"timeframe_15m":d15,"vwap":vwap,"support":levels["support"],"resistance":levels["resistance"],"core_conditions":reasons,"advanced_intelligence":adv,"timestamp":ts}


# ------------------------- ENGINE ----------------------------
def build_startup_messages():
    b="🤖 *KETS STRATEGY ENGINE ONLINE*\n━━━━━━━━━━━━━━━━━━\n✅ Backend connected\n📊 Timeframe: 1 minute\n🔄 Scan interval: 1 minute\n⏰ Trading hours: 06:00-18:00 EAT\n💰 Monday-Friday: GOLD ONLY\n₿ Weekend: BTC ONLY\n🧠 Advanced intelligence ON\n━━━━━━━━━━━━━━━━━━\nℹ️ Strength is strategy alignment, not guaranteed win probability."
    c="🤖 *KETS STRATEGY ENGINE ONLINE*\n━━━━━━━━━━━━━━━━━━\n✅ Signal system online\n📊 1-minute monitoring\n🔄 Analysis every 1 minute\n⏰ Active: 06:00-18:00 EAT\n⚡ Early-entry detection ON\n━━━━━━━━━━━━━━━━━━\n📡 KETS is monitoring the market."
    return b,c



def run_signal_source_bridge():
    """Legacy compatibility stub.

    Production no longer needs a signal bridge because the trading bot sends
    signals directly to POST /api/signals on this website. Kept only so older
    imports/configurations do not fail if they reference the function.
    """
    while True:
        time.sleep(3600)


def _signal_dt(item):
    raw=item.get("timestamp") or item.get("timestamp_utc") or item.get("created_at")
    try:
        dt=datetime.datetime.fromisoformat(str(raw).replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=EAT)
        return dt
    except Exception:
        return get_eat_time()


def run_strategy():
    global last_scan, next_scan
    token=os.environ.get("TELEGRAM_BOT_TOKEN"); bot_id=os.environ.get("TELEGRAM_CHAT_ID"); channel_id=os.environ.get("TELEGRAM_CHANNEL_ID"); key=os.environ.get("TWELVE_DATA_API_KEY")
    print("🚀 KETS Strategy Engine started — 1M / 1M")
    sb,sc=build_startup_messages(); send_to_bot_and_channel(token,bot_id,channel_id,sb,sc)
    next_scan=(get_eat_time()+datetime.timedelta(seconds=60)).isoformat()
    while True:
        started=time.time()
        try:
            now=get_eat_time(); last_scan=now.isoformat()
            if not trading_hours_open():
                sleep_time=60
                next_scan=(get_eat_time()+datetime.timedelta(seconds=sleep_time)).isoformat()
                time.sleep(sleep_time); continue
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
        sleep_time=max(1,60-(time.time()-started)); next_scan=(get_eat_time()+datetime.timedelta(seconds=sleep_time)).isoformat(); time.sleep(sleep_time)


# Gunicorn imports this module and does not execute __main__. Start the engine
# during module import, exactly once per worker.
engine_started = False
if os.environ.get("KETS_DISABLE_ENGINE", "1") != "1":
    engine_started = True
    Thread(target=run_strategy, daemon=True, name="kets-strategy-engine").start()

# The trading bot POSTs signals directly to this website. There is therefore
# no polling bridge in the production path; disabling it removes the extra
# connection that previously caused needless latency/timeouts.
source_bridge_started = False
autotrade_worker_started = False
if os.environ.get("KETS_DISABLE_AUTOTRADE", "0") != "1":
    autotrade_worker_started = True
    Thread(target=_ctrader_autotrade_loop, daemon=True, name="kets-ctrader-autotrade").start()

if __name__ == "__main__":
    run_strategy()
