KETS SUPABASE ACCOUNT CREATION FIX

Run KETS-main/KETS_SUPABASE_REPAIR_ALL.sql in the Supabase SQL Editor.
Then configure Render with:
- SUPABASE_URL
- SUPABASE_SECRET_KEY (preferred) OR SUPABASE_SERVICE_ROLE_KEY
- KETS_SESSION_SECRET

The publishable key is for browser/public use and must not be used as the backend's
trusted database-write credential when RLS is enabled.
