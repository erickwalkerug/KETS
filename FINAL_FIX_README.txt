KETS FINAL FIX

1. In Supabase SQL Editor, run SUPABASE_KETS_FINAL_SETUP.sql from this package.
2. In Render, set SUPABASE_URL and SUPABASE_SECRET_KEY.
   If you only have an older service-role JWT, set LEGACY_SERVICE_ROLE_KEY_NOT_USED instead.
3. Redeploy the service.
4. Test Create account again.

Expected result: a successful account is inserted into public.users.
If Supabase is temporarily unavailable, registration now returns a controlled 503
with a useful message instead of an unhandled HTTP 500.

Never expose the secret/service-role key to the browser.
