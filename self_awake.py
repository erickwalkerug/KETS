"""KETS keep-awake helper.

The Render web service can sleep when it has no incoming traffic. This helper
pings the public health endpoint periodically. It is also used by the bundled
GitHub Actions workflow, which is the preferred way to keep the service warm
without adding a second long-running process to the Render web service.
"""

import os
import time
import requests

KETS_URL = os.getenv("KETS_PUBLIC_URL", "https://kets.onrender.com").rstrip("/")
INTERVAL_SECONDS = int(os.getenv("KETS_KEEP_AWAKE_INTERVAL", "600"))


def ping() -> None:
    response = requests.get(f"{KETS_URL}/api/health", timeout=30)
    response.raise_for_status()
    print(f"KETS health OK: {response.status_code} {response.text[:200]}")


if __name__ == "__main__":
    while True:
        try:
            ping()
        except Exception as exc:
            print(f"KETS keep-awake ping failed: {exc}")
        time.sleep(INTERVAL_SECONDS)
