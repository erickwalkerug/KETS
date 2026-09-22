# KETS session scheduling

KETS now uses EAT (UTC+3) with two automated-trading windows:

- 06:00–11:00 EAT — ACTIVE
- 11:00–14:30 EAT — IDLE
- 14:30–17:30 EAT — ACTIVE
- 17:30–06:00 EAT — OUTSIDE HOURS

The dashboard reports the same authoritative session state. The cTrader Auto-Trader continues configured break-even/trailing protection for existing positions, but does not process new signal-driven automatic entries while IDLE or OUTSIDE HOURS. No forced position close is introduced by the schedule.

## Render Free wake-up

A sleeping Render Free web service cannot wake itself at 14:30 EAT. The repository therefore includes `.github/workflows/kets-session-wake.yml`, which sends health requests during the two active windows. Add these GitHub repository secrets:

- `KETS_PUBLIC_URL` — public KETS URL, e.g. `https://kets.onrender.com`
- `SIGNAL_PUBLIC_URL` — public signal-engine URL

The workflow uses UTC cron times because GitHub Actions schedules are UTC. It starts the second session at 14:30 EAT (11:30 UTC) and stops pings after 17:20 EAT (14:20 UTC), allowing Render's idle timeout to take effect after the 17:30 EAT session end.

GitHub Actions schedules can be delayed by the platform. The 14:20 EAT final pre-session ping and the 14:30 EAT wake ping provide a small operational margin, but this is not a hard real-time scheduler.
