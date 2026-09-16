# KETS cTrader setup

KETS uses cTrader Open API OAuth and executes only through the cTrader account the user authorizes. Exness/MT5 autotrading has been removed.

## Render environment variables
- `CTRADER_CLIENT_ID` — cTrader Open API application client ID
- `CTRADER_CLIENT_SECRET` — cTrader Open API application secret
- `CTRADER_REDIRECT_URI` — `https://kets.onrender.com/ctrader/callback`
- `KETS_SESSION_SECRET` — existing KETS session secret; keep it stable

Register this exact callback URL in the cTrader Open API application. The user signs into KETS first, presses **Connect cTrader**, approves trading access on cTrader, then selects a demo or live account.

## Automatic trading behavior
- Automatic trading is OFF until the user enables it.
- The engine checks for a complete KETS BUY/SELL plan, a low-risk classification (or a score at/above the configured minimum when no explicit risk label exists), and valid TP/SL.
- It does not force an entry when conditions are unfavorable. It waits for a later favorable signal.
- Lot size, allocation, maximum open trades, minimum quality, and the $25 projected-profit target remain available in the website controls.
- The $25 value is a planning target, not a guaranteed profit.

Use a demo account first. cTrader API execution depends on the broker's symbols, contract specifications, permissions, spread, slippage, and account type.
