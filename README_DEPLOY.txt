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
- FLW_SECRET_KEY = Flutterwave secret key (keep private)
- FLW_PUBLIC_KEY = optional if you later add hosted/inline checkout
- FLW_WEBHOOK_SECRET_HASH = webhook secret hash from Flutterwave dashboard
- KETS_SESSION_SECRET = long random secret used to sign KETS access tokens
- KETS_ACCESS = leave locked in production; set paid only for temporary owner/admin override

SECURITY
- bot.py contains the strategy and remains server-side.
- /api/signals is now protected by the server access switch.
- Unpaid users can see only the retained signal history, not live signals.
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
2. Customer enters email, Uganda phone number and MTN/Airtel.
3. KETS creates a Flutterwave Uganda mobile-money charge.
4. Customer approves the mobile-money prompt on the phone.
5. KETS server verifies the transaction using Flutterwave's transaction verification API.
6. Only a verified matching UGX payment unlocks live signals.
7. The browser receives a signed access token; the secret key never reaches the browser.

Before going live, create/verify the Flutterwave merchant account and complete required KYC/merchant approval. Configure the webhook URL in Flutterwave as:
https://YOUR-KETS-DOMAIN/api/payments/webhook


PRIVATE SIGNAL SOURCE
- KETS_SIGNAL_SOURCE_URL = https://my-btc-bot-l0xm.onrender.com
- KETS_SIGNAL_SOURCE_KEY = exactly the same random value configured as KETS_SIGNALS_API_KEY on the trading bot service
- KETS_DISABLE_ENGINE = 1 (recommended for the website service so it never runs a second strategy)

The browser never receives KETS_SIGNAL_SOURCE_KEY. The website backend uses it server-to-server.
