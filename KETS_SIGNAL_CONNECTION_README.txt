## Direct bot -> website signal bridge

The website accepts direct POST requests from the KETS trading bot at `/api/signals`. The bot sends JSON and the website normalizes the payload, rejects malformed requests, prevents duplicate signal IDs, stores the signal for the 7-day history, and makes it immediately available to the existing dashboard API. Signal authentication is optional by default so a Render secret mismatch cannot block delivery; set `KETS_REQUIRE_SIGNAL_AUTH=1` to require `X-KETS-API-KEY`.

Required WEBSITE Render variable:
`KETS_SIGNAL_RECEIVER_KEY=<same secret configured on the trading bot>`

For compatibility, the receiver also accepts the existing `KETS_SIGNAL_SOURCE_KEY`, `KETS_API_KEY`, or `KETS_SIGNALS_API_KEY` when `KETS_SIGNAL_RECEIVER_KEY` is not set.

Recommended WEBSITE Render variables:
`KETS_SIGNAL_RECEIVER_KEY=<same secret as bot>`
`KETS_DISABLE_ENGINE=1`
`KETS_DISABLE_SOURCE_BRIDGE=1`

The browser never receives the secret. The bot communicates server-to-server directly with:
`https://kets.onrender.com/api/signals`

The older pull-based source bridge remains in the code as an optional compatibility/fallback mechanism, but direct POST delivery is now the primary connection path.

## Permanent signal storage

Directly received signals are also written to the `signals` table in the same
Render PostgreSQL database selected by `DATABASE_URL` (SQLite is used locally
when Postgres is unavailable). Signal IDs are the unique idempotency key.
The database keeps the full signal payload permanently; the public dashboard
APIs expose the current 7-day signal window with live delivery for all
authenticated users. This storage is independent of the in-memory 7-day cache.


AUTOMATIC HISTORY IMPORT
-------------------------
The website source bridge is enabled in render.yaml and points to the live
KETS trading bot at https://my-btc-bot-l0xm.onrender.com. On startup and every
KETS_SOURCE_POLL_SECONDS (default 10 seconds), the website requests the bot's
authenticated /api/signals?limit=200 feed and imports the available 7-day
history into the permanent Render PostgreSQL signals table. Repeated imports
are safe because signal IDs are stored idempotently.

RENDER SECRETS
-------------
Set KETS_SIGNAL_SOURCE_KEY on the website to the trading bot's KETS_API_KEY.
Set KETS_SIGNAL_RECEIVER_KEY on the website to the same secret so the bot's
direct POSTs to /api/signals are accepted. Do not put the secret in GitHub.


ENGINE SCAN HISTORY
-------------------
The website consumes the trading bot's authenticated GET /api/engine-history
feed when available. This feed may contain both qualifying signals and scan
records whose status is "NO QUALIFYING SETUP". The dashboard renders those
records explicitly instead of treating them as missing signals. Actual
qualifying BUY/SELL records remain separate from no-setup scan records.
