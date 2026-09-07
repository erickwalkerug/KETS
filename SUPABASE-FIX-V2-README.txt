KETS SUPABASE FIX V2
====================

This package changes the KETS server-side Supabase connection.

1. Supabase SQL
---------------
Run:
  KETS-main/SUPABASE_REPAIR_V2.sql

It keeps the existing custom KETS tables:
  public.users
  public.payments
  public.signals

It does not delete existing rows.

2. Render environment variables
--------------------------------
Required:
  SUPABASE_URL=https://YOUR-PROJECT.supabase.co
  SUPABASE_SECRET_KEY=sb_secret_...

Optional legacy compatibility:
  SUPABASE_SERVICE_ROLE_KEY=eyJ...

Keep:
  SUPABASE_PUBLISHABLE_KEY=sb_publishable_...

IMPORTANT:
The publishable key is NOT the server database key. Do not put the secret/service-role
key in supabase.config.js, index.html, app.js, or any browser-visible file.

3. Deploy
---------
The server now uses:
  KETS-main/supabase_client.py

It:
- requires a server-only key for database operations;
- rejects accidental use of sb_publishable_* as the server key;
- supports modern sb_secret_* keys;
- supports legacy service-role JWT keys;
- retries transient 429/502/503/504 failures;
- returns safe diagnostic codes instead of exposing Supabase secrets/errors.

4. Test
-------
After Render deploys, open:
  /api/health

A working database should return HTTP 200 with:
  "code": "DB_OK"

If it returns 503, the response contains a safe code such as:
  DB_SERVER_KEY_MISSING
  DB_WRONG_KEY_TYPE
  DB_AUTH_OR_RLS
  DB_TABLE_OR_SCHEMA
  DB_SUPABASE_UNAVAILABLE
  DB_NETWORK

5. Sign in
----------
The normal KETS sign-in still uses the existing KETS custom users table and password
hashing. No passwords need to be migrated.

The frontend does NOT connect directly to Supabase for authentication. It calls:
  POST /api/auth/login

The Flask backend then reads public.users through Supabase REST.
