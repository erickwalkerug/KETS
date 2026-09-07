"""
KETS Supabase server client.

Server-side only:
- SUPABASE_URL
- SUPABASE_SECRET_KEY (preferred modern sb_secret_* key)
  OR SUPABASE_SERVICE_ROLE_KEY (legacy JWT key)

The browser publishable key is deliberately NOT accepted for database writes.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import requests


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

# Prefer the modern secret key, but keep legacy service-role compatibility.
SUPABASE_SERVER_KEY = SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY


class SupabaseIntegrityError(Exception):
    """Raised when PostgREST rejects a duplicate/conflicting row."""


class SupabaseResult:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class SupabaseConnection:
    """Small DB-API-like adapter for the existing KETS backend."""

    RETRY_STATUS = {429, 502, 503, 504}

    def __init__(self):
        if not SUPABASE_URL:
            raise RuntimeError("SUPABASE_URL is not configured.")
        if not SUPABASE_SERVER_KEY:
            raise RuntimeError(
                "SUPABASE_SECRET_KEY or SUPABASE_SERVICE_ROLE_KEY is not configured."
            )

        # A publishable key is safe for browser access, but must never be used
        # as the trusted server database credential.
        if SUPABASE_SERVER_KEY.startswith("sb_publishable_"):
            raise RuntimeError(
                "A publishable Supabase key cannot be used for server database access."
            )

        self.base = SUPABASE_URL + "/rest/v1"
        self.headers = {
            "apikey": SUPABASE_SERVER_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Legacy service-role keys are JWTs. Modern sb_secret_* keys should not
        # be sent as Bearer tokens.
        if SUPABASE_SERVER_KEY.count(".") == 2:
            self.headers["Authorization"] = f"Bearer {SUPABASE_SERVER_KEY}"

    def _request(self, method, table, params=None, payload=None, prefer=None):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer

        last_error = None
        for attempt in range(3):
            try:
                response = requests.request(
                    method,
                    f"{self.base}/{table}",
                    headers=headers,
                    params=params,
                    json=payload,
                    timeout=(8, 20),
                )
            except requests.RequestException as exc:
                last_error = RuntimeError(f"Supabase network error: {exc}")
                if attempt < 2:
                    time.sleep(0.7 * (attempt + 1))
                    continue
                raise last_error from exc

            if response.status_code in self.RETRY_STATUS and attempt < 2:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 5.0) if retry_after else 0.7 * (attempt + 1)
                except ValueError:
                    delay = 0.7 * (attempt + 1)
                time.sleep(delay)
                continue

            if response.status_code >= 400:
                text = response.text[:1000]
                low = text.lower()
                if (
                    response.status_code in (409, 422)
                    or "duplicate" in low
                    or "unique" in low
                    or "already exists" in low
                ):
                    raise SupabaseIntegrityError(text)
                raise RuntimeError(
                    f"Supabase request failed ({response.status_code}): {text}"
                )

            if not response.content:
                return []

            try:
                data = response.json()
            except ValueError:
                return []

            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
            return []

        raise last_error or RuntimeError("Supabase request failed after retries.")

    def execute(self, sql, params=()):
        q = " ".join(str(sql).strip().split())
        low = q.lower()
        params = tuple(params or ())

        if low.startswith("select"):
            table_m = re.search(r"from\s+(users|payments|signals)\b", low)
            if not table_m:
                raise RuntimeError(f"Unsupported Supabase SELECT: {q[:250]}")
            table = table_m.group(1)

            if "count(*) n" in low and table == "payments":
                rows = self._request(
                    "GET",
                    "payments",
                    {
                        "select": "plan,status,updated_at",
                        "status": "eq.COMPLETED",
                        "updated_at": "not.is.null",
                    },
                )
                counts = {}
                for row in rows:
                    plan = row.get("plan")
                    counts[plan] = counts.get(plan, 0) + 1
                return SupabaseResult([{"plan": k, "n": v} for k, v in counts.items()])

            select_m = re.search(r"select\s+(.*?)\s+from\s+", q, re.I)
            select_cols = select_m.group(1).strip() if select_m else "*"
            select_cols = select_cols.replace(" COLLATE NOCASE", "")
            api_params = {"select": select_cols}

            if table == "users":
                if "where id=" in low:
                    api_params["id"] = f"eq.{params[0]}"
                elif "where email=" in low:
                    # KETS normalizes newly registered emails to lowercase.
                    # Use exact matching first, then a case-insensitive fallback.
                    api_params["email"] = f"eq.{str(params[0]).lower()}"
            elif table == "payments":
                if "where user_id=" in low:
                    api_params["user_id"] = f"eq.{params[0]}"
                    if "status='completed'" in low:
                        api_params["status"] = "eq.COMPLETED"
                elif "where tx_ref=" in low:
                    api_params["tx_ref"] = f"eq.{params[0]}"
                elif "where status='completed'" in low:
                    api_params["status"] = "eq.COMPLETED"
            elif table == "signals":
                if "timestamp >=" in low:
                    api_params["timestamp"] = f"gte.{params[0]}"

            if "order by created_at desc" in low:
                api_params["order"] = "created_at.desc"
            elif "order by updated_at desc" in low:
                api_params["order"] = "updated_at.desc"
            elif "order by timestamp asc" in low:
                api_params["order"] = "timestamp.asc"

            if "limit 100" in low:
                api_params["limit"] = "100"
            elif "limit 500" in low:
                api_params["limit"] = "500"

            rows = self._request("GET", table, api_params)

            # Backward compatibility for accounts whose old email casing was
            # preserved in the database.
            if (
                table == "users"
                and not rows
                and "where email=" in low
                and params
            ):
                rows = self._request(
                    "GET",
                    table,
                    {"select": select_cols, "email": f"ilike.{params[0]}"},
                )

            return SupabaseResult(rows)

        if low.startswith("insert"):
            table_m = re.search(
                r"into\s+(users|payments|signals)\s*\((.*?)\)\s+values",
                q,
                re.I,
            )
            if not table_m:
                raise RuntimeError(f"Unsupported Supabase INSERT: {q[:300]}")
            table = table_m.group(1)
            columns = [c.strip() for c in table_m.group(2).split(",")]
            row = dict(zip(columns, params))

            if table == "users":
                self._request("POST", table, payload=row, prefer="return=minimal")
            elif table in ("payments", "signals"):
                self._request(
                    "POST",
                    table,
                    payload=row,
                    prefer="resolution=merge-duplicates,return=minimal",
                )
            return SupabaseResult([])

        if low.startswith("update users set"):
            values = {
                "name": params[0],
                "country_name": params[1],
                "country_code": params[2],
                "profile_picture": params[3],
                "updated_at": params[4],
            }
            self._request(
                "PATCH",
                "users",
                {"id": f"eq.{params[5]}"},
                values,
                prefer="return=minimal",
            )
            return SupabaseResult([])

        if low.startswith("update payments set"):
            if "updated_at=? where tx_ref=?" in low:
                values = {
                    "tracking_id": params[0],
                    "status": "COMPLETED",
                    "amount": params[1],
                    "updated_at": params[2],
                }
                tx_ref = params[3]
            else:
                values = {
                    "tracking_id": params[0],
                    "status": params[1],
                    "amount": params[2],
                }
                tx_ref = params[3]

            self._request(
                "PATCH",
                "payments",
                {"tx_ref": f"eq.{tx_ref}"},
                values,
                prefer="return=minimal",
            )
            return SupabaseResult([])

        raise RuntimeError(f"Unsupported Supabase query: {q[:350]}")

    def commit(self):
        pass

    def close(self):
        pass


def supabase_diagnostics():
    """Return safe diagnostics without exposing credentials."""
    if not SUPABASE_URL:
        return {"ok": False, "stage": "configuration", "code": "DB_NOT_CONFIGURED"}
    if not SUPABASE_SERVER_KEY:
        return {"ok": False, "stage": "configuration", "code": "DB_SERVER_KEY_MISSING"}
    if SUPABASE_SERVER_KEY.startswith("sb_publishable_"):
        return {"ok": False, "stage": "configuration", "code": "DB_WRONG_KEY_TYPE"}

    try:
        conn = SupabaseConnection()
        conn._request("GET", "users", {"select": "id", "limit": "1"})
        return {"ok": True, "stage": "database", "code": "DB_OK"}
    except Exception as exc:
        text = str(exc).lower()
        if "network error" in text or "timeout" in text:
            code = "DB_NETWORK"
        elif any(x in text for x in ("401", "403", "permission", "row-level security")):
            code = "DB_AUTH_OR_RLS"
        elif any(x in text for x in ("404", "relation", "schema cache")):
            code = "DB_TABLE_OR_SCHEMA"
        elif any(x in text for x in ("502", "503", "504")):
            code = "DB_SUPABASE_UNAVAILABLE"
        else:
            code = "DB_UNAVAILABLE"
        return {"ok": False, "stage": "database", "code": code}
