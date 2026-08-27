KETS DASHBOARD
==============

Files:
- index.html
- styles.css
- app.js
- manifest.json

SETUP
-----
1. Upload these frontend files to your GitHub frontend repository.
2. Set API_BASE in app.js by adding:
   localStorage.setItem("KETS_API_BASE", "https://YOUR-BACKEND.onrender.com");
   before the loadAll() call, OR change API_BASE directly.
3. If your backend has KETS_API_SECRET enabled, users should NOT receive the secret.
   For production, replace the placeholder frontend access flow with real user authentication
   and short-lived server-issued access tokens.
4. The current UI deliberately does not display EMA, RSI, MACD, ADX, DI, ATR, VWAP,
   support/resistance calculations, strategy weights, or source code.
5. The current payment screen is an interface only. Automatic payment recognition requires
   a real payment verification integration on the backend.

IMPORTANT
---------
The provided bot.py exposes /api/status, /api/history, /api/signals, /api/plans and /api/access.
Live signals remain locked until the backend says the user is paid.

For Android/iPhone download links, replace the website #android/#iphone sections with
the exact installation pages on your official website once those pages exist.
