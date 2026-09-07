-- KETS FINAL SUPABASE SETUP
-- Run once in Supabase SQL Editor. Safe to run on an existing empty deployment.

BEGIN;

CREATE TABLE IF NOT EXISTS public.users (
  id text PRIMARY KEY,
  email text NOT NULL UNIQUE,
  password_hash text NOT NULL,
  name text NOT NULL DEFAULT '',
  country_name text NOT NULL DEFAULT '',
  country_code text NOT NULL DEFAULT '',
  profile_picture text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.payments (
  id text PRIMARY KEY,
  user_id text NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
  tx_ref text NOT NULL UNIQUE,
  tracking_id text,
  plan text NOT NULL,
  amount numeric NOT NULL,
  currency text NOT NULL DEFAULT 'UGX',
  status text NOT NULL,
  network text,
  email text,
  phone text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.signals (
  id text PRIMARY KEY,
  asset text NOT NULL,
  direction text NOT NULL,
  score double precision NOT NULL,
  timestamp timestamptz NOT NULL,
  payload text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS users_email_idx ON public.users (lower(email));
CREATE INDEX IF NOT EXISTS payments_user_id_idx ON public.payments (user_id);
CREATE INDEX IF NOT EXISTS payments_status_updated_idx ON public.payments (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS signals_timestamp_idx ON public.signals (timestamp DESC);

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY;

COMMIT;

-- Verification
SELECT 'users' AS table_name, count(*) AS rows FROM public.users
UNION ALL SELECT 'payments', count(*) FROM public.payments
UNION ALL SELECT 'signals', count(*) FROM public.signals;
