-- KETS SUPABASE FINAL REPAIR V2
-- Run this entire file in Supabase SQL Editor.
-- This matches the current KETS backend: custom public.users + payments + signals.
-- It does NOT delete existing accounts or payments.

BEGIN;

CREATE TABLE IF NOT EXISTS public.users (
  id text PRIMARY KEY,
  email text NOT NULL UNIQUE,
  password_hash text NOT NULL DEFAULT '',
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
  amount numeric NOT NULL DEFAULT 0,
  currency text NOT NULL DEFAULT 'UGX',
  status text NOT NULL DEFAULT 'PENDING',
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
  score double precision NOT NULL DEFAULT 0,
  timestamp timestamptz NOT NULL DEFAULT now(),
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Repair older installations.
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS password_hash text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS country_name text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS country_code text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS profile_picture text;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS updated_at timestamptz;

ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS tracking_id text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS plan text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS amount numeric;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS currency text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS status text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS network text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS email text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS phone text;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS created_at timestamptz;
ALTER TABLE public.payments ADD COLUMN IF NOT EXISTS updated_at timestamptz;

ALTER TABLE public.signals ADD COLUMN IF NOT EXISTS asset text;
ALTER TABLE public.signals ADD COLUMN IF NOT EXISTS direction text;
ALTER TABLE public.signals ADD COLUMN IF NOT EXISTS score double precision;
ALTER TABLE public.signals ADD COLUMN IF NOT EXISTS timestamp timestamptz;
ALTER TABLE public.signals ADD COLUMN IF NOT EXISTS payload jsonb;
ALTER TABLE public.signals ADD COLUMN IF NOT EXISTS created_at timestamptz;

-- Fill nulls introduced by older schemas before adding NOT NULL constraints.
UPDATE public.users SET
  password_hash = COALESCE(password_hash, ''),
  name = COALESCE(name, ''),
  country_name = COALESCE(country_name, ''),
  country_code = COALESCE(country_code, ''),
  profile_picture = COALESCE(profile_picture, ''),
  created_at = COALESCE(created_at, now()),
  updated_at = COALESCE(updated_at, now());

UPDATE public.payments SET
  plan = COALESCE(plan, 'UNKNOWN'),
  amount = COALESCE(amount, 0),
  currency = COALESCE(currency, 'UGX'),
  status = COALESCE(status, 'PENDING'),
  created_at = COALESCE(created_at, now()),
  updated_at = COALESCE(updated_at, now());

UPDATE public.signals SET
  asset = COALESCE(asset, 'UNKNOWN'),
  direction = COALESCE(direction, 'NONE'),
  score = COALESCE(score, 0),
  timestamp = COALESCE(timestamp, now()),
  payload = COALESCE(payload, '{}'::jsonb),
  created_at = COALESCE(created_at, now());

CREATE INDEX IF NOT EXISTS users_email_idx ON public.users (lower(email));
CREATE INDEX IF NOT EXISTS payments_user_status_idx
  ON public.payments (user_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS payments_tx_ref_idx
  ON public.payments (tx_ref);
CREATE INDEX IF NOT EXISTS signals_timestamp_idx
  ON public.signals (timestamp DESC);

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payments ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY;

COMMIT;

SELECT
  (SELECT count(*) FROM public.users) AS users,
  (SELECT count(*) FROM public.payments) AS payments,
  (SELECT count(*) FROM public.signals) AS signals;
