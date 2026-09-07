IMPORTANT DEPLOYMENT NOTE (ACCOUNT CREATION FIX):
The browser publishable key is NOT sufficient for KETS backend registration/payment writes when
Supabase Row Level Security is enabled. Render must have SUPABASE_URL plus either
SUPABASE_SECRET_KEY (preferred) or SUPABASE_SERVICE_ROLE_KEY. Keep that key server-side only.
Run KETS_SUPABASE_REPAIR_ALL.sql once in Supabase SQL Editor.

# KETS — DIRECT BOT → WEBSITE → SUPABASE SIGNAL STORAGE

This version keeps the trading bot as the direct signal sender and makes
Supabase the LAST step.

SIGNAL FLOW
-----------
Trading Bot
   ↓ POST /api/signals
KETS Website
   ↓ _persist_signal()
Supabase public.signals

The website receives the signal first. It normalizes/stores it in its live
website cache and then writes the same complete signal payload to Supabase.
The browser never writes signals directly to Supabase.

IMPORTANT RENDER VARIABLES
--------------------------
KETS_DISABLE_ENGINE=1
KETS_DISABLE_SOURCE_BRIDGE=1
KETS_SIGNAL_RECEIVER_KEY=<same secret as the bot's KETS_API_KEY>
SUPABASE_URL + SUPABASE_PUBLISHABLE_KEY=<Supabase REST API connection string>

Do not put the Supabase password or service key in frontend JavaScript.

SUPABASE TABLE
--------------
Run SUPABASE_SIGNALS_REPAIR.sql in Supabase SQL Editor before deploying this
version if the existing public.signals table contains legacy NOT NULL columns.
The website stores the complete signal JSON in the payload column, while the
core columns are id, asset, direction, score, timestamp and created_at.

No trading strategy or market schedule is changed by this storage change.
