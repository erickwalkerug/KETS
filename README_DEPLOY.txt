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
- All signed-in users receive live signals. Paid plans remain available for the existing account/payment features.
- Current KETS_ACCESS is a server-wide switch. A real per-user payment/login system must be connected before selling access to multiple users.

WEBSITE FEATURES
- 6:00 AM–6:00 PM EAT signal window
- 2-minute strategy scan/update interval
- Monday-Friday: GOLD (XAU/USD) only
- Saturday-Sunday: BTC (BTC/USD) only
- Live BUY/SELL signal display when unlocked
- Market price
- Expected market move
- Signal duration/age
- Estimated duration
- Take profit / stop loss
- 7-day signal history
- Automatic 10-second dashboard refresh (engine scans every 1 minute)
- No app installation or service-worker dependency

PAYMENT FLOW
1. Customer chooses 1 Day, 1 Week, 1 Month or 1 Year.
2. Customer enters email, Uganda phone number and network preference.
3. KETS creates a Pesapal API 3.0 order in UGX.
4. Customer is sent to the secure Pesapal payment page.
5. Pesapal redirects the customer to the KETS callback URL.
6. KETS verifies the transaction with Pesapal GetTransactionStatus.
7. Pesapal also sends status-change notifications to the KETS IPN endpoint.
8. Verified payments unlock the existing paid-plan/account features; signal freshness is live for all signed-in users.

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

PRIVATE SIGNAL SOURCE / DIRECT PUSH
- The trading bot POSTs each generated signal to: https://kets.onrender.com/api/signals
- Set KETS_SIGNAL_RECEIVER_KEY on the WEBSITE to exactly the same secret used by the bot's X-KETS-API-KEY header.
- Set KETS_DISABLE_ENGINE = 1 on the WEBSITE so it never runs a second strategy engine.
- Set KETS_DISABLE_SOURCE_BRIDGE = 1 when using direct bot -> website POST delivery as the primary connection (recommended; avoids blocking login with history imports).
- The KETS_SIGNAL_SOURCE_URL / KETS_SIGNAL_SOURCE_KEY bridge is enabled for automatic history synchronization from the live trading bot; it imports the bot's existing 7-day signal feed into permanent PostgreSQL storage and continues syncing new history.

The browser never receives the receiver/source key. The website backend uses it server-to-server.


ACCOUNT / DASHBOARD UPGRADE
- Sign-in and account creation use email and password only. Google Sign-In has been removed.
- Email/password registration and sign-in.
- One email address can only have one KETS account.
- Required country selection with country name and country code.
- User profiles and profile pictures.
- Payment history tied to the signed-in account.
- Paid community counts for 30 minutes, 1 hour, 4 hours, 1 day, 1 week, 1 month and 1 year.
- All displayed prices are UGX.
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


RENDER CLOUD PERSISTENCE
The production account store is now Render Postgres. The Blueprint provisions a managed Postgres database named kets-db and injects its internal connection string into DATABASE_URL. Registered users, password hashes, profiles, payments, payment history, and subscription records are stored in Postgres instead of the web service's ephemeral filesystem. Render documents that Blueprint `fromDatabase` injects the database connection string into the service, and Render Postgres provides durable managed storage and recovery features.

IMPORTANT: the KETS web service and Postgres database must be in the same Render region when using the Blueprint's internal connection string. If the existing KETS web service is already in another region, create/move the Postgres database to that same region before syncing the Blueprint.

LOCAL FALLBACK
If DATABASE_URL is not set, KETS continues to use the local kets.db SQLite file for development/testing. Production should use DATABASE_URL.

EXISTING USER MIGRATION
If you have an older KETS kets.db containing registered users/payments, copy that database to a safe machine and run:
  KETS_DB_PATH=/path/to/kets.db DATABASE_URL='YOUR_RENDER_POSTGRES_URL' python migrate_sqlite_to_postgres.py
The migration is manual by design so old records are not silently duplicated or overwritten during deployment.

DEVELOPER SECURITY
Set KETS_DEVELOPER_USERNAME and KETS_DEVELOPER_PASSWORD as Render environment variables. Do not place them in HTML, JavaScript, GitHub, or public screenshots.


SIGNAL ACCESS RULES (UPDATED)
- Sign-up is free and stores the account in cloud Postgres.
- Normal users must have an active completed payment before sign-in is accepted.
- The developer account (KETS_DEVELOPER_USERNAME / KETS_DEVELOPER_PASSWORD) can sign in for free through the normal Sign In form.
- Every successfully signed-in user, including the developer, sees the same complete live-signal interface; there is no separate Developer Dashboard.
- Live signal prices and signal targets are always displayed in USD ($).
- Payment plans can use UGX for Uganda and USD for international users.
- Payment expiry is enforced server-side and shown as a live countdown.
- The website refreshes its live data every 10 seconds with a countdown.
- Paid users retain their subscription/payment history in the account dashboard.
- The website reads the live signal snapshot from KETS_SIGNAL_SOURCE_URL when configured.

SELF-AWAKE / KEEP-ALIVE
- A GitHub Actions workflow is included at .github/workflows/kets-keep-awake.yml.
- It pings https://kets.onrender.com/api/health every 10 minutes and can also be started manually from GitHub Actions.
- The health endpoint already exists in bot.py and is configured as Render's healthCheckPath.
- If you use a custom domain, change KETS_URL in the workflow to your public KETS URL.
- self_awake.py is also included for manual use outside Render. Do not run it as a second Render web service; the bundled GitHub Actions workflow is the preferred keep-awake method.
- Keep-alive cannot guarantee 100% uptime if Render suspends the service or the external scheduler is disabled; it simply generates regular traffic to reduce idle sleeping.


ENGINE HISTORY UPDATE
- The dashboard now recognizes bot engine-history records marked NO QUALIFYING SETUP.
- Engine History displays those scans explicitly rather than showing an empty signal row.
- Qualifying BUY/SELL signals remain unchanged.
