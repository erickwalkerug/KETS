"""One-time migration of KETS users/payments from the old SQLite store to Render Postgres.

Usage (on a machine that has the old kets.db):
  KETS_DB_PATH=/path/to/kets.db DATABASE_URL='postgresql://...' python migrate_sqlite_to_postgres.py

The script is intentionally manual so an accidental deploy cannot overwrite or
silently duplicate production account data.
"""
import os
import sqlite3

import psycopg
from psycopg.rows import dict_row

SQLITE_PATH = os.environ.get("KETS_DB_PATH", os.path.join(os.path.dirname(__file__), "kets.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required")
if not os.path.exists(SQLITE_PATH):
    raise SystemExit(f"SQLite database not found: {SQLITE_PATH}")

src = sqlite3.connect(SQLITE_PATH)
src.row_factory = sqlite3.Row
pg = psycopg.connect(DATABASE_URL, row_factory=dict_row)

try:
    users = src.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    payments = src.execute("SELECT * FROM payments ORDER BY created_at").fetchall()

    with pg.cursor() as cur:
        for row in users:
            cur.execute(
                """INSERT INTO users
                (id,email,password_hash,name,country_name,country_code,profile_picture,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET
                  email=EXCLUDED.email,
                  password_hash=EXCLUDED.password_hash,
                  name=EXCLUDED.name,
                  country_name=EXCLUDED.country_name,
                  country_code=EXCLUDED.country_code,
                  profile_picture=EXCLUDED.profile_picture,
                  updated_at=EXCLUDED.updated_at""",
                tuple(row[k] for k in ("id","email","password_hash","name","country_name","country_code","profile_picture","created_at","updated_at")),
            )

        for row in payments:
            cur.execute(
                """INSERT INTO payments
                (id,user_id,tx_ref,tracking_id,plan,amount,currency,status,network,email,phone,created_at,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (tx_ref) DO UPDATE SET
                  tracking_id=EXCLUDED.tracking_id,
                  plan=EXCLUDED.plan,
                  amount=EXCLUDED.amount,
                  currency=EXCLUDED.currency,
                  status=EXCLUDED.status,
                  network=EXCLUDED.network,
                  email=EXCLUDED.email,
                  phone=EXCLUDED.phone,
                  updated_at=EXCLUDED.updated_at""",
                tuple(row[k] for k in ("id","user_id","tx_ref","tracking_id","plan","amount","currency","status","network","email","phone","created_at","updated_at")),
            )

    pg.commit()
    print(f"Migrated {len(users)} users and {len(payments)} payments to Postgres.")
finally:
    src.close()
    pg.close()
