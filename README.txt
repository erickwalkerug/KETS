KETS FIXED DEPLOYMENT

Upload all files in this folder to the SAME Render web service/repository:
- bot.py
- index.html
- app.js
- styles.css
- manifest.json
- service-worker.js
- requirements.txt
- render.yaml

Recommended Render start command:
gunicorn bot:app --workers 1 --threads 4 --timeout 120

Required environment variable:
TWELVE_DATA_API_KEY

Optional:
KETS_API_SECRET
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
TELEGRAM_CHANNEL_ID

Important:
- The dashboard and API are served by the same Render service, so the frontend uses the same origin by default.
- The root URL serves index.html rather than JSON.
- The engine is started when Gunicorn imports bot:app.
- Indicators remain backend-only.
