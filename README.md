KETS — 2-minute app project
Official refresh interval: 120 seconds (2 minutes).
The frontend calls:
GET /api/signals
GET /api/status
Render backend: https://my-btc-bot-l0xm.onrender.com
Important
The supplied bot.py currently exposes only /, so the backend must add the two API routes before the app can receive signals.
The payment buttons are UI placeholders until a real payment provider/account and callback/webhook are connected. Do not treat a localStorage unlock as proof of payment.
No 10-second refresh
There is no 10-second refresh timer in this project. The app polling interval is 120000 ms.
