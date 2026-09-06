-- KETS Supabase signal-storage repair
-- Fixes the 23502 NOT NULL failure caused by the bot inserting a signal
-- without values for legacy required columns.
--
-- Run this in Supabase SQL Editor AFTER backing up the signals table.

DO $$
DECLARE
  r record;
BEGIN
  -- Make legacy nullable columns optional where they are not part of the
  -- website's required signal API contract.
  FOR r IN
    SELECT c.column_name
    FROM information_schema.columns c
    JOIN information_schema.tables t
      ON t.table_schema = c.table_schema
     AND t.table_name = c.table_name
    WHERE c.table_schema = 'public'
      AND c.table_name = 'signals'
      AND c.is_nullable = 'NO'
      AND c.column_name NOT IN ('id','asset','direction','score','timestamp','payload','created_at')
      AND c.column_default IS NULL
      AND c.is_identity = 'NO'
  LOOP
    EXECUTE format('ALTER TABLE public.signals ALTER COLUMN %I DROP NOT NULL', r.column_name);
  END LOOP;
END $$;

-- Ensure the website API's core fields are present and usable.
ALTER TABLE public.signals
  ALTER COLUMN id SET NOT NULL,
  ALTER COLUMN asset SET NOT NULL,
  ALTER COLUMN direction SET NOT NULL,
  ALTER COLUMN score SET NOT NULL,
  ALTER COLUMN timestamp SET NOT NULL,
  ALTER COLUMN created_at SET NOT NULL;

-- Helpful index for newest signals.
CREATE INDEX IF NOT EXISTS signals_timestamp_idx
  ON public.signals (timestamp DESC);

-- Verify the resulting schema.
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema='public' AND table_name='signals'
ORDER BY ordinal_position;
