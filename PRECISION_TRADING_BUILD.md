# KETS — Precision Exness MT5 Automatic Trading Build

This is an upgraded replacement for the previous KETS Exness/MT5 managed-trading build.

## What was improved
- Custom lot size on the KETS Managed Trading page.
- Default lot remains 0.01 for safety.
- Configurable minimum entry-quality threshold (80/85/90/95).
- Strong-reversal filter enabled by default.
- Fresh-signal execution gate: automatic entries only use signals no older than 180 seconds.
- Maximum open-trade setting.
- $25 projected-profit target field for planning; it is not a guaranteed profit.
- MT5 EA normalizes volume to the broker's minimum/maximum/step.
- MT5 EA reports live positions, entry, SL, TP, volume and floating P/L to KETS.
- Optional opposite-position close before a new opposite-direction signal.
- Existing KETS public signal logic, pages, schedules and other setups are preserved.

## Important
No trading strategy can be made perfectly accurate or guaranteed profitable. The upgrade is designed to make execution more selective and disciplined, not to promise a win rate or minimum dollar profit.

## Files
- `bot.py` — KETS backend and managed-trading API.
- `app.js` — Managed Trading controls.
- `index.html` — Managed Trading settings UI.
- `KETS_MT5_Exness_Bridge.mq5` — MT5 execution bridge.
- `EXNESS_MT5_SETUP.md` — setup instructions.
