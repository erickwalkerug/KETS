# KETS Signal Connection

This package preserves the existing KETS rules and connects the website to the trading signal API.

Signal flow:
Trading Bot -> KETS Signal API -> Website -> User access rules

Existing website rules are preserved, including:
- live signals for authorized/paid users
- delayed signals for users who are not entitled to live access
- existing country/currency and payment logic
- existing Telegram functionality
- existing developer/admin interface

Render environment variables for the WEBSITE service:
KETS_SIGNAL_SOURCE_URL=https://YOUR-BOT-RENDER-URL
KETS_SIGNAL_SOURCE_KEY=YOUR_KETS_API_KEY

The source URL should point to the Render service running the trading bot/API.
The key must be the same value as KETS_API_KEY on the trading bot service.

Do not put the private API key in browser/client-side code.
