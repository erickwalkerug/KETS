
## Automatic source bridge
The website now runs a server-side signal-source bridge in the background. It polls the configured source every 10 seconds and caches `/api/history` and `/api/signals`, so delivery does not depend on a browser request. It logs each source request and exposes non-secret diagnostics at `/api/source-status`.

Required WEBSITE Render variables:
KETS_SIGNAL_SOURCE_URL=https://my-btc-bot-l0xm.onrender.com
KETS_SIGNAL_SOURCE_KEY=<same secret as the trading bot API key>

Optional:
KETS_SOURCE_POLL_SECONDS=10
