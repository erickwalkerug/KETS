-- KETS: make public.signals compatible with the website's direct receiver.
-- Run this ONCE in Supabase SQL Editor.
-- It does not change trading rules.

BEGIN;

-- Legacy columns not used by the receiver must not block a valid signal insert.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.column_name
    FROM information_schema.columns c
    WHERE c.table_schema='public'
      AND c.table_name='signals'
      AND c.is_nullable='NO'
      AND c.column_name NOT IN
          ('id','asset','direction','score','timestamp','payload','created_at')
      AND c.column_default IS NULL
  LOOP
    EXECUTE format(
      'ALTER TABLE public.signals ALTER COLUMN %I DROP NOT NULL',
      r.column_name
    );
  END LOOP;
END $$;

-- Core fields used by KETS.
ALTER TABLE public.signals
  ALTER COLUMN id SET NOT NULL,
  ALTER COLUMN asset SET NOT NULL,
  ALTER COLUMN direction SET NOT NULL,
  ALTER COLUMN score SET NOT NULL,
  ALTER COLUMN timestamp SET NOT NULL,
  ALTER COLUMN payload SET NOT NULL,
  ALTER COLUMN created_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS signals_timestamp_idx
  ON public.signals (timestamp DESC);

COMMIT;

SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema='public' AND table_name='signals'
ORDER BY ordinal_position;
