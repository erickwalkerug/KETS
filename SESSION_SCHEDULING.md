# KETS session scheduling

KETS uses EAT (UTC+3) with the saved working schedule:

- Monday-Friday: 06:00–11:00 EAT — ACTIVE
- Monday-Friday: 11:00–15:00 EAT — IDLE / market scanning paused
- Monday-Friday: 15:00–18:00 EAT — ACTIVE
- Monday-Friday: outside those windows — OUTSIDE HOURS / market scanning paused
- Saturday-Sunday: OFFLINE

The schedule is persisted in the KETS database metadata and mirrored in the trading bot's persistent settings, so a normal restart/redeploy does not reset the approved hours.

The schedule pauses new signal scanning/entries outside the active windows. Existing cTrader positions are not force-closed merely because a session ends; configured position-protection logic remains available through the Auto-Trader worker.
