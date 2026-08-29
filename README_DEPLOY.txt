KETS WEBSITE VERSION

This package removes the Android/PWA installation dependency. KETS is now a normal responsive website that users open in Chrome.

DEPLOYMENT
1. Upload these files to the GitHub repository connected to your Render Web Service.
2. Render will use render.yaml and start: gunicorn app:app --workers 1 --threads 4 --timeout 120
3. Open the Render URL in Chrome.

IMPORTANT ENVIRONMENT VARIABLES
- TWELVE_DATA_API_KEY = your private Twelve Data key
- TELEGRAM_BOT_TOKEN = optional Telegram bot token
- TELEGRAM_CHAT_ID = optional Telegram destination
- TELEGRAM_CHANNEL_ID = optional Telegram channel
- PESAPAL_CONSUMER_KEY = Pesapal consumer key (keep private)
- PESAPAL_CONSUMER_SECRET = Pesapal consumer secret (keep private)
- PESAPAL_IPN_ID = 5d7821a0-86d3-4b4c-8970-d9f848086aaa (already registered)
- PESAPAL_ENVIRONMENT = live for production, sandbox for testing
- KETS_PUBLIC_URL = https://kets.onrender.com unless you use a custom domain
- KETS_SESSION_SECRET = long random secret used to sign KETS access tokens
- KETS_ACCESS = leave locked in production; set paid only for temporary owner/admin override

SECURITY
- bot.py contains the strategy and remains server-side.
- /api/signals is now protected by the server access switch.
- Paid users receive live signals. Unpaid users receive the same signals only after a server-enforced 30-minute delay.
- Current KETS_ACCESS is a server-wide switch. A real per-user payment/login system must be connected before selling access to multiple users.

WEBSITE FEATURES
- 6:00 AM–6:00 PM EAT signal window
- Live BUY/SELL signal display when unlocked
- Market price
- Expected market move
- Signal duration/age
- Estimated duration
- Take profit / stop loss
- 7-day signal history
- Automatic 10-second website refresh
- No app installation or service-worker dependency

PAYMENT FLOW
1. Customer chooses 1 Day, 1 Week, 1 Month or 1 Year.
2. Customer enters email, Uganda phone number and network preference.
3. KETS creates a Pesapal API 3.0 order in UGX.
4. Customer is sent to the secure Pesapal payment page.
5. Pesapal redirects the customer to the KETS callback URL.
6. KETS verifies the transaction with Pesapal GetTransactionStatus.
7. Pesapal also sends status-change notifications to the KETS IPN endpoint.
8. Only a verified matching UGX payment unlocks live signals.

PESAPAL URLS
- IPN URL: https://kets.onrender.com/api/payments/ipn
- Callback URL: https://kets.onrender.com/api/payments/callback
- Cancel URL: https://kets.onrender.com/api/payments/cancel

IPN REGISTRATION
1. Deploy this version first.
2. Set PESAPAL_CONSUMER_KEY, PESAPAL_CONSUMER_SECRET, PESAPAL_ENVIRONMENT and KETS_PUBLIC_URL in Render.
3. The KETS IPN is already registered in Pesapal.
4. In Render, set PESAPAL_IPN_ID to: 5d7821a0-86d3-4b4c-8970-d9f848086aaa
5. Redeploy/restart the service.
The registered KETS IPN URL is: https://kets.onrender.com/api/payments/ipn

The IPN URL must be publicly reachable. Pesapal API 3.0 requires an IPN to be registered before submitting orders.

PRIVATE SIGNAL SOURCE
- KETS_SIGNAL_SOURCE_URL = https://my-btc-bot-l0xm.onrender.com
- KETS_SIGNAL_SOURCE_KEY = exactly the same random value configured as KETS_SIGNALS_API_KEY on the trading bot service
- KETS_DISABLE_ENGINE = 1 (recommended for the website service so it never runs a second strategy)

The browser never receives KETS_SIGNAL_SOURCE_KEY. The website backend uses it server-to-server.


ACCOUNT / DASHBOARD UPGRADE
- Sign-in and account creation use email and password only. Google Sign-In has been removed.
- Email/password registration and sign-in.
- One email address can only have one KETS account.
- Required country selection with country name and country code.
- User profiles and profile pictures.
- Payment history tied to the signed-in account.
- Paid community counts for 30 minutes, 1 hour, 4 hours, 1 day, 1 week, 1 month and 1 year.
- All displayed prices are UGX.
- Developer interface: /developer, protected by KETS_DEVELOPER_USERNAME and KETS_DEVELOPER_PASSWORD.
- Developer data is served only after developer authentication.
- Support email: kets2026ug@gmail.com.

PLANS
- UGX 500 = 30 minutes
- UGX 1,000 = 1 hour
- UGX 3,000 = 4 hours
- UGX 5,000 = 1 day (existing KETS price)
- UGX 30,000 = 1 week (existing KETS price)
- UGX 100,000 = 1 month (existing KETS price)
- UGX 1,000,000 = 1 year (existing KETS price)


RENDER PERSISTENCE
The account database is SQLite. For production, attach a Render persistent disk and set KETS_DB_PATH to a path on that disk (for example /var/data/kets.db). Without persistent storage, account/payment data can be lost when the service is rebuilt or its ephemeral filesystem is replaced.

DEVELOPER SECURITY
Set KETS_DEVELOPER_USERNAME and KETS_DEVELOPER_PASSWORD as Render environment variables. Do not place them in HTML, JavaScript, GitHub, or public screenshots.


SIGNAL ACCESS RULES (UPDATED)
- Paid users: live signal feed with no intentional delay while their subscription is active.
- Unpaid users: signal feed and 7-day history are filtered server-side to signals at least 30 minutes old.
- The 30-minute delay is enforced by the Flask backend, not by browser JavaScript, so changing the frontend cannot reveal live signals.
- The website reads delayed history from KETS_SIGNAL_SOURCE_URL when configured, while paid users read the live /api/signals feed.

SELF-AWAKE / KEEP-ALIVE
- A GitHub Actions workflow is included at .github/workflows/kets-keep-awake.yml.
- It pings https://kets.onrender.com/api/health every 10 minutes and can also be started manually from GitHub Actions.
- The health endpoint already exists in bot.py and is configured as Render's healthCheckPath.
- If you use a custom domain, change KETS_URL in the workflow to your public KETS URL.
- self_awake.py is also included for manual use outside Render. Do not run it as a second Render web service; the bundled GitHub Actions workflow is the preferred keep-awake method.
- Keep-alive cannot guarantee 100% uptime if Render suspends the service or the external scheduler is disabled; it simply generates regular traffic to reduce idle sleeping.
