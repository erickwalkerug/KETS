KETS WEBSITE - PROP FIRM FIXED

This package preserves the existing website/trading rules and Prop Firm branding.

IMPORTANT:
The Render logs showed:
Supabase signals write failed: HTTP 400 / PostgreSQL 23502

That is a Supabase schema NOT NULL mismatch. The website API itself returned HTTP 201,
so the bot -> website API path is working.

Before relying on Supabase as permanent storage:
1. Open Supabase SQL Editor.
2. Open SUPABASE_SIGNALS_REPAIR.sql from this ZIP.
3. Run it.
4. Restart/redeploy the bot on Render.
5. Confirm the next signal no longer produces "23502".

No trading strategy, BTC/GOLD schedule, or scan interval is changed by this repair.
