# KETS cTrader Open API connection

This build adds the cTrader OAuth callback without changing the existing KETS signal engine or MT5 connector.

## Render environment variables

Add these to the KETS Render service:

- `CTRADER_CLIENT_ID` = the Client ID shown under the KETS cTrader application's **Credentials**.
- `CTRADER_CLIENT_SECRET` = the Client Secret shown under **Credentials**. Keep this private.
- `CTRADER_REDIRECT_URI` = `https://kets.onrender.com/ctrader/callback`
- `KETS_PUBLIC_BASE_URL` = `https://kets.onrender.com` (already used by the site).

The existing `KETS_SESSION_SECRET` is used to protect OAuth state and encrypt the cTrader access/refresh tokens stored by KETS.

## cTrader portal

For the KETS application, register this exact redirect URI:

`https://kets.onrender.com/ctrader/callback`

Use the `trading` permission when users are intended to authorize trading. cTrader's authorization code is short-lived, so the callback immediately exchanges it for the access and refresh tokens.

## Important

The first cTrader build establishes secure OAuth authorization and token storage. Account discovery, account selection, symbol mapping, and live order execution are a separate Open API layer and should be enabled only after the KETS cTrader application is approved and the OAuth connection has been verified.

Never put `CTRADER_CLIENT_SECRET` in `index.html`, `app.js`, GitHub, or the browser.

Build note: this package implements OAuth authorization and secure token storage. It does not yet place live orders from cTrader; that requires the next Open API account/symbol/order execution layer.
